#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_probe.py - Phase 1 : validation de la chaine de synchro tosu.

Ce script ne fait QUE de l'observation, il n'ecrit rien (sauf --save-dir) :

  1. se connecte a ws://127.0.0.1:24050/websocket/v2, avec reconnexion auto
  2. logue les transitions d'etat et les changements de map selectionnee
  3. quand le checksum de la map reste stable --debounce secondes, telecharge
     le .osu courant via GET http://127.0.0.1:24050/files/beatmap/file
  4. verifie que md5(fichier) == beatmap.checksum, puis passe le fichier a
     rosu-pp et affiche stars / aim / speed / circles / sliders / max_combo

AUCUN nom d'etat n'est code en dur. On declenche sur tout changement de
checksum et on note dans quel etat ca s'est produit ; le resume affiche a
l'arret (Ctrl+C) donne la valeur a retenir pour la song select.

L'identite d'une map est son checksum MD5, jamais son beatmap_id (les maps
unsubmitted ont id = 0).

Usage :
    python scripts/sync_probe.py
    python scripts/sync_probe.py --raw                  # logue chaque message ws
    python scripts/sync_probe.py --save-dir data/live   # garde les .osu fetches
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- dependances -------------------------------------------------------------
try:
    from websockets.asyncio.client import connect as ws_connect  # websockets >= 13
except Exception:
    try:
        from websockets.client import connect as ws_connect      # websockets < 13
    except Exception:
        ws_connect = None

try:
    import rosu_pp_py as rosu
except Exception:
    rosu = None


# --- utilitaires -------------------------------------------------------------

def ts() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    """Acces imbrique tolerant : dig(payload, "beatmap", "mode", "name")."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def fmt_val(val: Any) -> str:
    if isinstance(val, float):
        return f"{val:.4f}"
    if isinstance(val, (list, tuple)):
        return f"<{type(val).__name__} n={len(val)}>"
    s = repr(val)
    return s if len(s) <= 60 else s[:57] + "..."


def http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "osu-mapmatcher-sync-probe/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


@dataclass(frozen=True)
class MapSnap:
    checksum: str
    beatmap_id: int
    set_id: int
    artist: str
    title: str
    version: str
    mapper: str
    mode_name: str
    mode_number: int
    state_name: str
    state_number: int
    file_rel: str
    stars_tosu: float
    aim_tosu: float
    speed_tosu: float

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}]"


# --- rosu-pp -----------------------------------------------------------------

def build_beatmap(raw: bytes):
    """rosu_pp_py.Beatmap accepte bytes= ou content= selon la version."""
    errors = []
    for key, value in (("bytes", raw), ("content", raw.decode("utf-8", "replace"))):
        try:
            return rosu.Beatmap(**{key: value})
        except TypeError as exc:
            errors.append(f"{key}=: {exc}")
    raise TypeError("aucun constructeur Beatmap accepte -> " + " | ".join(errors))


def introspect(attrs: Any, strains: Any) -> None:
    """Dump unique de la surface reelle de rosu-pp (prepare la phase 2)."""
    line = "-" * 72
    print(f"  {line}")
    print("  INTROSPECTION rosu-pp (affichee une seule fois)")
    print(f"  rosu_pp_py version : {getattr(rosu, '__version__', '?')}")
    print(f"  {line}")
    print("  DifficultyAttributes :")
    for name in sorted(dir(attrs)):
        if name.startswith("_"):
            continue
        try:
            val = getattr(attrs, name)
        except Exception as exc:
            print(f"     {name:<34} <erreur: {exc}>")
            continue
        if callable(val):
            continue
        print(f"     {name:<34} {fmt_val(val)}")
    print("  Strains :")
    for name in sorted(dir(strains)):
        if name.startswith("_"):
            continue
        try:
            val = getattr(strains, name)
        except Exception as exc:
            print(f"     {name:<34} <erreur: {exc}>")
            continue
        if callable(val):
            continue
        if isinstance(val, (list, tuple)):
            if len(val):
                print(f"     {name:<34} PEUPLE   n={len(val):<6} "
                      f"min={min(val):.3f} max={max(val):.3f}")
            else:
                print(f"     {name:<34} VIDE")
        else:
            print(f"     {name:<34} {fmt_val(val)}")
    print(f"  {line}")


def analyse(raw: bytes):
    """Retourne (ligne resume, attrs, strains)."""
    bm = build_beatmap(raw)
    diff = rosu.Difficulty()
    attrs = diff.calculate(bm)
    strains = diff.strains(bm)

    def g(name: str) -> Any:
        return getattr(attrs, name, None)

    def num(name: str, spec: str = ".2f") -> str:
        val = g(name)
        return format(val, spec) if isinstance(val, (int, float)) else "?"

    summary = (
        f"{num('stars')}*  aim={num('aim')}  speed={num('speed')}  "
        f"circles={g('n_circles')}  sliders={g('n_sliders')}  "
        f"spinners={g('n_spinners')}  combo={g('max_combo')}"
    )
    return summary, attrs, strains


# --- probe -------------------------------------------------------------------

class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        base = f"{args.host}:{args.port}"
        self.ws_url = f"ws://{base}/websocket/v2"
        self.file_url = f"http://{base}/files/beatmap/file"

        self.msg_count = 0
        self.state_counts: Counter = Counter()
        self.errors: Counter = Counter()
        self.map_events: list = []

        self._last_state = None
        self._confirmed_checksum = None
        self._pending_checksum = None
        self._debounce_task = None
        self._introspected = False

        if args.save_dir:
            Path(args.save_dir).mkdir(parents=True, exist_ok=True)

    # -- boucle de connexion --------------------------------------------------

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                log(f"connexion a {self.ws_url} ...")
                async with ws_connect(
                    self.ws_url, max_size=None, ping_interval=None, open_timeout=5
                ) as ws:
                    log("connecte. Clique sur des maps dans osu!. Ctrl+C pour le resume.")
                    backoff = 1.0
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[type(exc).__name__] += 1
                log(f"deconnecte : {type(exc).__name__}: {exc}")
            log(f"nouvelle tentative dans {backoff:.1f}s ...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 5.0)

    async def _pump(self, ws) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.args.stall_timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"aucun message depuis {self.args.stall_timeout}s (osu! ferme ?)"
                )
            self.msg_count += 1
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self.errors["json_decode"] += 1
                continue
            self._on_payload(payload)

    # -- traitement d'un message ---------------------------------------------

    def _on_payload(self, payload: dict) -> None:
        state_name = dig(payload, "state", "name", default="?")
        state_number = dig(payload, "state", "number", default=-1)
        checksum = (dig(payload, "beatmap", "checksum", default="") or "").strip()
        file_rel = dig(payload, "directPath", "beatmapFile", default="") or ""

        self.state_counts[(state_number, state_name)] += 1

        if self._last_state != (state_number, state_name):
            if self._last_state is not None:
                log(f"etat : {self._last_state[1]} ({self._last_state[0]}) "
                    f"-> {state_name} ({state_number})")
            else:
                log(f"etat initial : {state_name} ({state_number})")
            self._last_state = (state_number, state_name)

        if self.args.raw:
            log(
                f"raw state={state_name} id={dig(payload, 'beatmap', 'id')} "
                f"checksum={checksum[:8]} "
                f"artist={dig(payload, 'beatmap', 'artist')!r} "
                f"title={dig(payload, 'beatmap', 'title')!r} "
                f"version={dig(payload, 'beatmap', 'version')!r} "
                f"file={file_rel!r}"
            )

        if not checksum:
            return
        if checksum == self._confirmed_checksum or checksum == self._pending_checksum:
            return

        # nouveau checksum -> on (re)demarre le debounce
        self._pending_checksum = checksum
        # beatmap.stats.stars est un objet {live, total, aim, speed, ...}.
        # `live` vaut 0 hors gameplay -> on retombe sur `total`.
        def stat(key: str) -> float:
            val = dig(payload, "beatmap", "stats", "stars", key, default=None)
            return float(val) if isinstance(val, (int, float)) else 0.0

        stars = stat("live") or stat("total")
        snap = MapSnap(
            checksum=checksum,
            beatmap_id=dig(payload, "beatmap", "id", default=0) or 0,
            set_id=dig(payload, "beatmap", "set", default=0) or 0,
            artist=dig(payload, "beatmap", "artist", default="") or "",
            title=dig(payload, "beatmap", "title", default="") or "",
            version=dig(payload, "beatmap", "version", default="") or "",
            mapper=dig(payload, "beatmap", "mapper", default="") or "",
            mode_name=dig(payload, "beatmap", "mode", "name", default="?"),
            mode_number=dig(payload, "beatmap", "mode", "number", default=-1),
            state_name=state_name,
            state_number=state_number,
            file_rel=file_rel,
            stars_tosu=stars,
            aim_tosu=stat("aim"),
            speed_tosu=stat("speed"),
        )
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced(snap))

    async def _debounced(self, snap: MapSnap) -> None:
        try:
            await asyncio.sleep(self.args.debounce)
        except asyncio.CancelledError:
            return
        self._confirmed_checksum = snap.checksum
        self._pending_checksum = None
        try:
            await self._handle_map(snap)
        except Exception as exc:
            self.errors[f"handle:{type(exc).__name__}"] += 1
            log(f"  ERREUR traitement map : {type(exc).__name__}: {exc}")

    # -- map confirmee --------------------------------------------------------

    async def _handle_map(self, snap: MapSnap) -> None:
        self.map_events.append(
            {"state": snap.state_name, "checksum": snap.checksum,
             "id": snap.beatmap_id, "label": snap.label}
        )
        print("-" * 78, flush=True)
        log(f"MAP  etat={snap.state_name}({snap.state_number})  "
            f"checksum={snap.checksum}  id={snap.beatmap_id}  set={snap.set_id}  "
            f"mode={snap.mode_name}({snap.mode_number})")
        print(f"        {snap.label}   -- mapper: {snap.mapper}", flush=True)
        print(f"        fichier : {snap.file_rel}", flush=True)
        print(f"        tosu    : {snap.stars_tosu:.2f}*  aim={snap.aim_tosu:.2f}  "
              f"speed={snap.speed_tosu:.2f}   (reference, doit coller au rosu ci-dessous)",
              flush=True)

        # 1. fetch du .osu brut servi par tosu
        t0 = time.perf_counter()
        raw = None
        for attempt in (1, 2):
            try:
                raw = await asyncio.to_thread(http_get, self.file_url, self.args.http_timeout)
                break
            except Exception as exc:
                if attempt == 2:
                    self.errors[f"fetch:{type(exc).__name__}"] += 1
                    print(f"        fetch   : ECHEC {type(exc).__name__}: {exc}", flush=True)
                    return
                await asyncio.sleep(0.2)
        fetch_ms = (time.perf_counter() - t0) * 1000

        md5 = hashlib.md5(raw).hexdigest()
        match = "OK" if md5 == snap.checksum else f"MISMATCH (fichier={md5})"
        print(f"        fetch   : {len(raw)} octets en {fetch_ms:.0f} ms  |  md5 {match}",
              flush=True)

        if self.args.save_dir:
            dest = Path(self.args.save_dir) / f"{snap.checksum}.osu"
            dest.write_bytes(raw)
            print(f"        sauve   : {dest}", flush=True)

        # 2. rosu-pp
        if rosu is None:
            print("        rosu    : rosu_pp_py non installe -- pip install rosu-pp-py",
                  flush=True)
            return
        t1 = time.perf_counter()
        try:
            summary, attrs, strains = await asyncio.to_thread(analyse, raw)
        except Exception as exc:
            self.errors[f"rosu:{type(exc).__name__}"] += 1
            print(f"        rosu    : ECHEC {type(exc).__name__}: {exc}", flush=True)
            return
        calc_ms = (time.perf_counter() - t1) * 1000
        print(f"        rosu    : {summary}  (calc {calc_ms:.0f} ms)", flush=True)

        if not self._introspected and self.args.introspect:
            self._introspected = True
            introspect(attrs, strains)

    # -- resume ---------------------------------------------------------------

    def print_summary(self) -> None:
        print()
        print("=" * 78)
        print("RESUME DE LA SESSION")
        print("=" * 78)
        print(f"messages websocket recus : {self.msg_count}")
        print(f"changements de map       : {len(self.map_events)}")
        print()
        print("state.name observes (number, name -> nb de messages) :")
        for (num, name), count in self.state_counts.most_common():
            print(f"   {num:>3}  {name:<26} {count}")
        print()
        if self.map_events:
            print("etat actif au moment de chaque changement de map :")
            for name, count in Counter(e["state"] for e in self.map_events).most_common():
                print(f"        {name:<26} {count}")
            print()
            print("maps detectees :")
            for ev in self.map_events:
                print(f"        {ev['checksum']}  id={ev['id']:<9} {ev['label']}")
            print()
            print(">> le state.name de la song select est celui qui domine la liste"
                  " 'etat actif au moment de chaque changement de map'.")
        else:
            print("Aucun changement de map detecte : clique sur des maps differentes"
                  " pendant que le probe tourne.")
        if self.errors:
            print()
            print("erreurs :", dict(self.errors))
        print("=" * 78)


# --- entree ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe de synchro tosu (phase 1)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=24050)
    p.add_argument("--debounce", type=float, default=0.3,
                   help="secondes de stabilite du checksum avant declenchement")
    p.add_argument("--http-timeout", type=float, default=8.0)
    p.add_argument("--stall-timeout", type=float, default=15.0,
                   help="reconnecte si aucun message ws pendant N secondes")
    p.add_argument("--raw", action="store_true",
                   help="logue CHAQUE message websocket (tres verbeux)")
    p.add_argument("--save-dir", default=None,
                   help="dossier ou sauver les .osu fetches")
    p.add_argument("--no-introspect", dest="introspect", action="store_false",
                   help="ne pas dumper la surface de rosu-pp sur la premiere map")
    return p.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    if ws_connect is None:
        print("ERREUR: le paquet 'websockets' est introuvable. -> pip install websockets")
        return 1
    if rosu is None:
        print("AVERTISSEMENT: 'rosu_pp_py' introuvable, la partie difficulte sera"
              " sautee. -> pip install rosu-pp-py")

    probe = Probe(args)
    try:
        asyncio.run(probe.run())
    except KeyboardInterrupt:
        pass
    finally:
        probe.print_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
