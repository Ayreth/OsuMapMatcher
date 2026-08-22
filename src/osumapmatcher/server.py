#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - serveur local FastAPI : watcher tosu, API de recherche, page unique.

Le SSE ne pousse que le changement de map, pas les recommandations : les poids
et les filtres vivent dans le navigateur, c'est donc lui qui redemande une
recherche avec ses propres reglages. En local, l'aller-retour est gratuit et ca
evite au serveur d'avoir a suivre l'etat de chaque onglet ouvert.

Deux moteurs derriere la meme page :

  * `remote` (defaut) : la recherche part sur l'API hebergee. Le joueur n'a
    alors aucun index a telecharger -- c'est tout l'interet.
  * `local` : l'index est sur la machine, rien ne sort du reseau.

C'est ce serveur-ci qui parle au distant, pas le navigateur : il a deja le
checksum, les mods et le .osu que tosu lui a servi, et ca evite au passage
toute question de CORS. Le repli est automatique et dit a voix haute : si le
distant tombe et qu'un index local existe, la recherche continue en local et la
page l'affiche.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .api import ApiConfig, SearchService, build_router
from .features import GROUP_NAMES
from .index import DEFAULT_STAR_RANGE, QueryTarget, Searcher, SearcherSet
from .paths import index_root
from .remote import DEFAULT_API_URL, RemoteBackend, RemoteError
from .tosu import CurrentMap, TosuWatcher

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SSE_KEEPALIVE_S = 20.0

MODES = ("remote", "local")


@dataclass
class AppState:
    searchers: SearcherSet | None = None
    watcher: TosuWatcher | None = None
    current: CurrentMap | None = None
    remote: RemoteBackend | None = None
    #: le moteur demande (par l'utilisateur ou par l'environnement)
    wanted: str = "remote"
    #: celui qui a reellement servi la derniere recherche
    active: str = "remote"
    #: pourquoi on n'est pas sur le moteur demande, en clair
    fallback_reason: str = ""
    subscribers: set[asyncio.Queue] = None  # type: ignore[assignment]
    search_lock: Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.subscribers = set()
        self.search_lock = Lock()

    @property
    def has_local(self) -> bool:
        return bool(self.searchers and self.searchers.available())

    @property
    def has_remote(self) -> bool:
        return self.remote is not None

    def broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # un client trop lent perd un evenement, pas grave

    def backend_state(self) -> dict[str, Any]:
        return {
            "wanted": self.wanted,
            "active": self.active,
            "fallback_reason": self.fallback_reason,
            "api_url": self.remote.base_url if self.remote else "",
            "has_local": self.has_local,
            "has_remote": self.has_remote,
        }

    def note_backend(self, active: str, reason: str = "") -> None:
        """Enregistre le moteur utilise et previent la page s'il a change."""
        if (active, reason) == (self.active, self.fallback_reason):
            return
        self.active = active
        self.fallback_reason = reason
        self.broadcast({"type": "backend", "backend": self.backend_state()})


state = AppState()


def create_app(
    index_dir: Path | None = None,
    *,
    api_url: str | None = None,
    mode: str | None = None,
) -> FastAPI:
    index_dir = Path(index_dir) if index_dir else index_root()
    # une chaine vide desactive le distant pour de bon (`OSUMAPMATCHER_API_URL=`),
    # alors que l'absence de valeur laisse le defaut : les deux doivent se
    # distinguer, sinon on ne peut pas dire "jamais de reseau"
    if api_url is not None:
        resolved_url = api_url.strip()
    else:
        resolved_url = os.environ.get("OSUMAPMATCHER_API_URL", DEFAULT_API_URL).strip()
    resolved_mode = (mode or os.environ.get("OSUMAPMATCHER_MODE") or "remote").strip().lower()
    if resolved_mode not in MODES:
        resolved_mode = "remote"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.searchers = SearcherSet(index_dir)
        found = state.searchers.available()
        state.remote = RemoteBackend(resolved_url) if resolved_url else None
        state.wanted = state.active = resolved_mode
        state.fallback_reason = ""

        if found:
            log.info("index local : %s", ", ".join(found))
        if state.remote is not None:
            reachable = await asyncio.to_thread(state.remote.probe)
            log.info("api distante %s : %s", state.remote.base_url,
                     "ok" if reachable else state.remote.last_error)

        # Ni index ni serveur : la page n'aurait rien a montrer, autant le dire
        # tout de suite plutot que de laisser l'utilisateur chercher pourquoi
        # chaque recherche echoue.
        if not found and not state.has_remote:
            raise RuntimeError(
                f"No index in {index_dir} and no API configured. Run start.bat, "
                "build an index with scripts/build_all.py, or set OSUMAPMATCHER_API_URL."
            )
        if resolved_mode == "local" and not found:
            if state.has_remote:
                state.note_backend("remote", "no local index installed")
                state.wanted = "remote"
            else:
                raise RuntimeError(f"No index in {index_dir}.")

        async def on_map(snapshot: CurrentMap) -> None:
            state.current = snapshot
            state.broadcast({"type": "current", "current": snapshot.as_dict()})
            log.info("map : %s (%s)", snapshot.label,
                     "ok" if snapshot.features else snapshot.skip_reason)

        state.watcher = TosuWatcher(on_map)
        task = asyncio.create_task(state.watcher.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if state.searchers:
                state.searchers.close()

    app = FastAPI(title="osu!MapMatcher", lifespan=lifespan)

    # Le serveur local monte le MEME routeur que l'API hebergee (api.py), sans
    # quota ni jeton : un seul protocole a maintenir, et la page peut taper
    # /api/v1 ici ou a distance sans savoir lequel des deux elle a en face.
    #
    # Le CORS ouvert aux origines locales est ce qui permet au counter tosu --
    # servi par tosu, donc sur un autre port -- d'utiliser cet index plutot que
    # l'API publique. C'est le mode "tout en local" avec l'overlay de tosu.
    local_config = ApiConfig.local()
    if local_config.cors_regex:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=local_config.cors_regex,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-API-Key"],
            max_age=86400,
        )
    app.include_router(
        build_router(SearchService(lambda: state.searchers, local_config))
    )

    # -- page ------------------------------------------------------------------

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/explore")
    def explore_page() -> FileResponse:
        """
        La page du site, servie ici aussi bien que par l'API hebergee.

        Servie en local, elle interroge l'index local : c'est ce qui la rend
        utilisable sans le moindre acces reseau. Elle choisit son serveur toute
        seule, en commencant par sa propre origine.
        """
        return FileResponse(STATIC_DIR / "explore.html")

    # -- etat courant ----------------------------------------------------------

    @app.get("/api/current")
    def api_current() -> dict[str, Any]:
        variant, searcher = _active_searcher()
        corpus = _corpus_info(variant, searcher)
        return {
            "connected": bool(state.watcher and state.watcher.connected),
            "error": state.watcher.last_error if state.watcher else "watcher absent",
            "current": state.current.as_dict() if state.current else None,
            "groups": list(GROUP_NAMES),
            # la banniere prend son image de fond directement chez tosu, en local
            "tosu_url": state.watcher.base_url if state.watcher else "",
            "backend": state.backend_state(),
            **corpus,
        }

    # -- choix du moteur -------------------------------------------------------

    @app.post("/api/mode")
    async def api_mode(mode: str = Body(..., embed=True)) -> dict[str, Any]:
        """Bascule serveur <-> local depuis la page."""
        wanted = (mode or "").strip().lower()
        if wanted not in MODES:
            raise HTTPException(400, f"Unknown mode {mode!r}. Use one of {MODES}.")
        if wanted == "local" and not state.has_local:
            raise HTTPException(
                409,
                "No index on this machine. Start with --build, or run "
                "scripts/fetch_index.py, to search locally.",
            )
        if wanted == "remote" and not state.has_remote:
            raise HTTPException(409, "No API configured (OSUMAPMATCHER_API_URL is empty).")

        state.wanted = wanted
        if wanted == "remote" and state.remote is not None:
            # nouvelle tentative immediate : l'utilisateur vient de la demander
            await asyncio.to_thread(state.remote.probe)
        state.note_backend(wanted, "")
        return state.backend_state()

    # -- recherche -------------------------------------------------------------

    @app.get("/api/similar")
    async def api_similar(
        checksum: str | None = None,
        k: int = Query(20, ge=1, le=100),
        star_range: float = Query(DEFAULT_STAR_RANGE, ge=0.0, le=10.0),
        length_min: float = Query(0.5, ge=0.05, le=1.0),
        length_max: float = Query(2.0, ge=1.0, le=20.0),
        exclude_same_set: bool = True,
        max_per_song: int = Query(1, ge=0, le=10),
        diverge: str | None = None,
        allow_unranked: bool = True,
        w_aim: float = Query(1.0, ge=0.0, le=2.0),
        w_speed: float = Query(1.0, ge=0.0, le=2.0),
        w_rhythm: float = Query(1.0, ge=0.0, le=2.0),
        w_spacing: float = Query(1.0, ge=0.0, le=2.0),
        w_parameters: float = Query(1.0, ge=0.0, le=2.0),
    ) -> dict[str, Any]:
        options = {
            "k": k,
            "star_range": star_range,
            "length_min": length_min,
            "length_max": length_max,
            "exclude_same_set": exclude_same_set,
            "max_per_song": max_per_song,
            "diverge": diverge or None,
            "allow_unranked": allow_unranked,
            "w_aim": w_aim, "w_speed": w_speed, "w_rhythm": w_rhythm,
            "w_spacing": w_spacing, "w_parameters": w_parameters,
        }

        use_remote = (
            state.wanted == "remote"
            and state.remote is not None
            # apres un echec on laisse le serveur tranquille un moment, mais
            # seulement s'il y a de quoi chercher ici en attendant
            and not (state.remote.cooling_down and state.has_local)
        )

        note = ""
        if use_remote:
            try:
                payload = await _search_remote(checksum, options)
                state.note_backend("remote", "")
                payload["backend"] = state.backend_state()
                return payload
            except HTTPException:
                raise
            except RemoteError as exc:
                unknown_map = exc.status == 404
                if not state.has_local:
                    if unknown_map:
                        raise HTTPException(
                            404,
                            "This map is not in the server's corpus, and there is "
                            "no local index to fall back on.",
                        ) from exc
                    raise HTTPException(
                        502, f"Server unavailable: {exc.as_text()}"
                    ) from exc
                if unknown_map:
                    # le serveur va bien, c'est la map qu'il ne connait pas :
                    # on regarde dans l'index local sans changer de moteur ni
                    # alarmer la page
                    note = "not in the server's corpus, answered from the local index"
                else:
                    log.warning("bascule en local : %s", exc.as_text())
                    state.note_backend("local", exc.summary())
            except Exception as exc:  # jamais de 500 muet
                log.exception("recherche distante : echec inattendu")
                if not state.has_local:
                    raise HTTPException(
                        502, f"Unexpected failure: {type(exc).__name__}"
                    ) from exc
                state.note_backend("local", f"unexpected {type(exc).__name__}")
        elif state.wanted == "remote" and state.remote is not None:
            state.note_backend("local", state.remote.last_error or "server unavailable")
        else:
            state.note_backend("local", "")

        try:
            payload = await asyncio.to_thread(_search_local, checksum, options)
        except HTTPException:
            raise
        except Exception as exc:  # la trace part dans les logs, pas dans le vide
            log.exception("recherche locale : echec inattendu")
            raise HTTPException(500, f"Unexpected failure: {type(exc).__name__}") from exc
        if note:
            payload["warnings"] = [*payload.get("warnings", []), note]
        payload["backend"] = state.backend_state()
        return payload

    # -- flux d'evenements -----------------------------------------------------

    @app.get("/events")
    async def events() -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        state.subscribers.add(queue)
        if state.current is not None:
            queue.put_nowait({"type": "current", "current": state.current.as_dict()})

        async def stream():
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # garde la connexion ouverte
                        continue
                    yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
            finally:
                state.subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# --- recherche distante -------------------------------------------------------

async def _search_remote(checksum: str | None, options: dict[str, Any]) -> dict[str, Any]:
    """
    Envoie la question au serveur : le checksum suffit presque toujours.

    Le repli (map absente du corpus distant) est gere par RemoteBackend, avec
    ce qu'on peut lui donner d'ici : les features deja calculees, ou le .osu
    brut si le serveur n'a pas le meme jeu de features que nous.
    """
    assert state.remote is not None
    current = state.current
    wanted = checksum or (current.checksum if current else "")
    if not wanted:
        raise HTTPException(404, "No map selected in osu! yet.")

    # les features et le fichier ne servent que si la map interrogee est bien
    # celle qui est selectionnee -- sinon on n'a que son checksum
    same_map = bool(current and current.checksum == wanted)
    features = current.features if same_map else None
    raw = current.raw if same_map else None
    mods = current.mods if current else ""
    rate = current.rate if current else 1.0

    if same_map and features is None and raw is None and current.skip_reason:
        raise HTTPException(422, f"This map cannot be used: {current.skip_reason}.")

    return await asyncio.to_thread(
        state.remote.search,
        checksum=wanted,
        mods=mods,
        rate=rate,
        options=options,
        features=features,
        raw=raw,
    )


# --- recherche locale ---------------------------------------------------------

def _search_local(checksum: str | None, options: dict[str, Any]) -> dict[str, Any]:
    if state.searchers is None:
        raise HTTPException(503, "Index not loaded.")

    variant, searcher = _active_searcher()
    target = _resolve_target(searcher, checksum)
    if target is None:
        raise HTTPException(
            404,
            "No usable map. Pick an osu!standard map in osu!, or pass a "
            "checksum that exists in the corpus.",
        )

    weights = {
        "aim": options["w_aim"], "speed": options["w_speed"],
        "rhythm": options["w_rhythm"], "spacing": options["w_spacing"],
        "parameters": options["w_parameters"],
    }
    try:
        with state.search_lock:
            matches = searcher.search(
                target,
                k=options["k"],
                star_range=options["star_range"],
                length_ratio=(options["length_min"], options["length_max"]),
                exclude_same_set=options["exclude_same_set"],
                weights=weights,
                max_per_song=options["max_per_song"],
                diverge=options["diverge"],
                allow_unranked=options.get("allow_unranked", True),
            )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "query": {
            "checksum": target.checksum,
            "beatmap_id": target.beatmap_id,
            "label": target.label,
            "stars": round(target.stars, 2),
            "bpm": round(target.bpm, 1),
            "length_s": round(target.length_s, 1),
            "in_corpus": _in_corpus(searcher, target.checksum),
            "mods": state.current.mods if state.current else "",
        },
        "variant": variant,
        "diverge": options["diverge"],
        "count": len(matches),
        "matches": [match.as_dict() for match in matches],
        "source": "corpus",
        "warnings": [],
    }


# --- resolution de la cible ---------------------------------------------------

def _active_searcher() -> tuple[str, Searcher | None]:
    """
    Le corpus a interroger depend des mods actifs dans osu!.

    Le choix vaut pour toute la session, pas seulement pour la map courante :
    si tu joues en DT et que tu rebondis sur une reco, tu veux rester dans le
    corpus DT.
    """
    if state.searchers is None or not state.searchers.available():
        return "nomod", None
    mods = state.current.mods if state.current else ""
    return state.searchers.for_mods(mods)


def _corpus_info(variant: str, searcher: Searcher | None) -> dict[str, Any]:
    """Taille et variantes du corpus reellement interroge."""
    if state.active == "remote" and state.remote is not None:
        try:
            info = state.remote.info()
        except RemoteError:
            info = None
        if info is not None:
            from .features import variant_for_mods

            mods = state.current.mods if state.current else ""
            wanted = variant_for_mods(mods)
            if wanted not in info.variants:
                wanted = "nomod"
            return {
                "variant": wanted,
                "variants": info.variants,
                "n_maps": int(info.n_maps.get(wanted, 0)),
            }
    return {
        "variant": variant,
        "variants": state.searchers.available() if state.searchers else [],
        "n_maps": int(searcher.index.size) if searcher else 0,
    }


def _in_corpus(searcher: Searcher | None, checksum: str) -> bool:
    return bool(searcher) and searcher.has_checksum(checksum)


def _resolve_target(searcher: Searcher | None, checksum: str | None) -> QueryTarget | None:
    """
    Une map peut etre dans le corpus, ou seulement live (unsubmitted, editee).
    Dans le premier cas on lit son vecteur deja calcule, dans le second on
    featurise a la volee -- meme fonction et memes mods, donc meme vecteur.
    """
    if searcher is None:
        return None
    wanted = checksum or (state.current.checksum if state.current else None)
    if not wanted:
        return None

    target = searcher.target_from_checksum(wanted)
    if target is not None:
        return target

    current = state.current
    if current and current.checksum == wanted and current.features is not None:
        return searcher.target_from_features(current.features)
    return None


# --- point d'entree -----------------------------------------------------------

def main() -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serveur local osu!MapMatcher")
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=MODES, default=None,
                        help="moteur de recherche (defaut : remote)")
    parser.add_argument("--api", default=None, help="URL de l'API hebergee")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(
        create_app(args.index, api_url=args.api, mode=args.mode),
        host=args.host, port=args.port, log_level="warning",
    )
    return 0
