#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tosu.py - client du websocket tosu et recuperation du .osu courant.

Version tache de fond de ce que `scripts/sync_probe.py` a valide en phase 1 :
  * reconnexion automatique avec backoff ;
  * debounce sur le checksum, sinon scroller dans la song select declenche une
    featurisation par frame ;
  * l'identite d'une map est son checksum MD5, jamais son beatmap_id (les maps
    non soumises ont id = 0) ;
  * `selectPlay` n'est pas code en dur : on reagit a tout changement de checksum,
    quel que soit l'ecran.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 24050
DEFAULT_DEBOUNCE = 0.3
STALL_TIMEOUT = 15.0
HTTP_TIMEOUT = 8.0


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


@dataclass
class CurrentMap:
    checksum: str
    beatmap_id: int
    beatmapset_id: int
    artist: str
    title: str
    version: str
    mapper: str
    mode: str
    state: str
    stars: float
    length_s: float
    bpm: float
    mods: str = ""
    rate: float = 1.0
    variant: str = "nomod"
    features: dict[str, Any] | None = None
    skip_reason: str | None = None
    detected_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}]"

    @property
    def mods_active(self) -> bool:
        return bool(self.mods) or self.rate != 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "checksum": self.checksum,
            "beatmap_id": self.beatmap_id,
            "beatmapset_id": self.beatmapset_id,
            "artist": self.artist,
            "title": self.title,
            "version": self.version,
            "mapper": self.mapper,
            "mode": self.mode,
            "state": self.state,
            "mods": self.mods,
            "rate": round(self.rate, 3),
            "variant": self.variant,
            "stars": round(self.stars, 2),
            "length_s": round(self.length_s, 1),
            "bpm": round(self.bpm, 1),
            "usable": self.features is not None,
            "skip_reason": self.skip_reason,
            "url_web": f"https://osu.ppy.sh/b/{self.beatmap_id}" if self.beatmap_id else "",
        }


def http_get(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "osureco/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class TosuWatcher:
    """Surveille la map selectionnee et appelle `on_map` a chaque changement."""

    def __init__(
        self,
        on_map: Callable[[CurrentMap], Awaitable[None]],
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        debounce: float = DEFAULT_DEBOUNCE,
    ) -> None:
        self.on_map = on_map
        self.debounce = debounce
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/websocket/v2"
        self.file_url = f"{self.base_url}/files/beatmap/file"
        self.connected = False
        self.last_error: str | None = None
        self._confirmed: str | None = None
        self._pending: str | None = None
        self._task: asyncio.Task | None = None

    # -- boucle de connexion --------------------------------------------------

    async def run(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # websockets < 13
            from websockets.client import connect  # type: ignore

        backoff = 1.0
        while True:
            try:
                async with connect(
                    self.ws_url, max_size=None, ping_interval=None, open_timeout=5
                ) as websocket:
                    self.connected = True
                    self.last_error = None
                    log.info("tosu connecte sur %s", self.ws_url)
                    backoff = 1.0
                    await self._pump(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("tosu deconnecte : %s", self.last_error)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 5.0)

    async def _pump(self, websocket) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=STALL_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(f"aucun message depuis {STALL_TIMEOUT}s")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            self._on_payload(payload)

    # -- detection ------------------------------------------------------------

    def _on_payload(self, payload: dict) -> None:
        checksum = (dig(payload, "beatmap", "checksum", default="") or "").strip()
        if not checksum:
            return

        mods = (dig(payload, "play", "mods", "name", default="") or "").strip()
        rate = dig(payload, "play", "mods", "rate", default=1.0)
        rate = float(rate) if isinstance(rate, (int, float)) and rate > 0 else 1.0

        # La cle d'identite inclut les mods : activer DT sans changer de map doit
        # relancer la featurisation, sinon on recommande sur la mauvaise base.
        key = f"{checksum}|{mods}|{rate:.4f}"
        if key in (self._confirmed, self._pending):
            return
        self._pending = key

        def stat(key: str) -> float:
            value = dig(payload, "beatmap", "stats", "stars", key, default=None)
            return float(value) if isinstance(value, (int, float)) else 0.0

        snapshot = CurrentMap(
            checksum=checksum,
            beatmap_id=dig(payload, "beatmap", "id", default=0) or 0,
            beatmapset_id=dig(payload, "beatmap", "set", default=0) or 0,
            artist=dig(payload, "beatmap", "artist", default="") or "",
            title=dig(payload, "beatmap", "title", default="") or "",
            version=dig(payload, "beatmap", "version", default="") or "",
            mapper=dig(payload, "beatmap", "mapper", default="") or "",
            mode=dig(payload, "beatmap", "mode", "name", default="?"),
            state=dig(payload, "state", "name", default="?"),
            stars=stat("live") or stat("total"),
            length_s=0.0,
            bpm=float(dig(payload, "beatmap", "stats", "bpm", "common", default=0.0) or 0.0),
            mods=mods,
            rate=rate,
        )

        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._settle(snapshot, key))

    async def _settle(self, snapshot: CurrentMap, key: str) -> None:
        try:
            await asyncio.sleep(self.debounce)
        except asyncio.CancelledError:
            return
        self._confirmed = key
        self._pending = None
        try:
            await self._featurize(snapshot)
            await self.on_map(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("echec du traitement de la map %s", snapshot.checksum)

    async def _featurize(self, snapshot: CurrentMap) -> None:
        from .features import featurize_detailed, variant_for_mods

        try:
            raw = await asyncio.to_thread(http_get, self.file_url)
        except Exception as exc:
            snapshot.skip_reason = f"fetch:{type(exc).__name__}"
            return

        # On featurise avec les mods CANONIQUES de la variante, pas avec les
        # mods bruts : le corpus DT a ete bati avec "DT", et comparer un vecteur
        # calcule en "HDDT" a ce corpus introduirait un biais silencieux.
        snapshot.variant = variant_for_mods(snapshot.mods)
        canonical = None if snapshot.variant == "nomod" else snapshot.variant
        result = await asyncio.to_thread(featurize_detailed, raw, canonical)
        snapshot.features = result.features
        snapshot.skip_reason = result.skip_reason
        if result.features is not None:
            # on prefere nos propres valeurs : ce sont celles qui ont servi a
            # construire le corpus, donc les seules comparables
            snapshot.stars = float(result.features["stars"])
            snapshot.length_s = float(result.features["drain_time_s"])
            snapshot.bpm = float(result.features["bpm_median"])
            if not snapshot.beatmap_id:
                snapshot.beatmap_id = int(result.features["beatmap_id"])
