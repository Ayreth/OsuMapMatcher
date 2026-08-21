#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - serveur local FastAPI : watcher tosu, API de recherche, page unique.

Le SSE ne pousse que le changement de map, pas les recommandations : les poids
et les filtres vivent dans le navigateur, c'est donc lui qui redemande une
recherche avec ses propres reglages. En local, l'aller-retour est gratuit et ca
evite au serveur d'avoir a suivre l'etat de chaque onglet ouvert.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .features import GROUP_NAMES
from .index import DEFAULT_STAR_RANGE, QueryTarget, Searcher, SearcherSet
from .paths import index_root
from .tosu import CurrentMap, TosuWatcher

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SSE_KEEPALIVE_S = 20.0


@dataclass
class AppState:
    searchers: SearcherSet | None = None
    watcher: TosuWatcher | None = None
    current: CurrentMap | None = None
    subscribers: set[asyncio.Queue] = None  # type: ignore[assignment]
    search_lock: Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.subscribers = set()
        self.search_lock = Lock()

    def broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # un client trop lent perd un evenement, pas grave


state = AppState()


def create_app(index_dir: Path | None = None) -> FastAPI:
    index_dir = Path(index_dir) if index_dir else index_root()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.searchers = SearcherSet(index_dir)
        found = state.searchers.available()
        if not found:
            raise RuntimeError(
                f"No index found in {index_dir}. Run start.bat, or build one "
                "yourself with: python scripts/build_all.py"
            )
        log.info("index disponibles : %s (%s maps en nomod)",
                 ", ".join(found), state.searchers.get("nomod").index.size)

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

    app = FastAPI(title="osureco", lifespan=lifespan)

    # -- page ------------------------------------------------------------------

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # -- etat courant ----------------------------------------------------------

    @app.get("/api/current")
    def api_current() -> dict[str, Any]:
        variant, searcher = _active_searcher()
        return {
            "connected": bool(state.watcher and state.watcher.connected),
            "error": state.watcher.last_error if state.watcher else "watcher absent",
            "current": state.current.as_dict() if state.current else None,
            "groups": list(GROUP_NAMES),
            # la banniere prend son image de fond directement chez tosu, en local
            "tosu_url": state.watcher.base_url if state.watcher else "",
            "variant": variant,
            "variants": state.searchers.available() if state.searchers else [],
            "n_maps": int(searcher.index.size) if searcher else 0,
        }

    # -- recherche -------------------------------------------------------------

    @app.get("/api/similar")
    def api_similar(
        checksum: str | None = None,
        k: int = Query(20, ge=1, le=100),
        star_range: float = Query(DEFAULT_STAR_RANGE, ge=0.0, le=10.0),
        length_min: float = Query(0.5, ge=0.05, le=1.0),
        length_max: float = Query(2.0, ge=1.0, le=20.0),
        exclude_same_set: bool = True,
        max_per_song: int = Query(1, ge=0, le=10),
        diverge: str | None = None,
        w_aim: float = Query(1.0, ge=0.0, le=2.0),
        w_speed: float = Query(1.0, ge=0.0, le=2.0),
        w_rhythm: float = Query(1.0, ge=0.0, le=2.0),
        w_spacing: float = Query(1.0, ge=0.0, le=2.0),
        w_parameters: float = Query(1.0, ge=0.0, le=2.0),
    ) -> dict[str, Any]:
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
            "aim": w_aim, "speed": w_speed, "rhythm": w_rhythm,
            "spacing": w_spacing, "parameters": w_parameters,
        }
        try:
            with state.search_lock:
                matches = searcher.search(
                    target,
                    k=k,
                    star_range=star_range,
                    length_ratio=(length_min, length_max),
                    exclude_same_set=exclude_same_set,
                    weights=weights,
                    max_per_song=max_per_song,
                    diverge=diverge or None,
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
            "diverge": diverge or None,
            "count": len(matches),
            "matches": [match.as_dict() for match in matches],
        }

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


# --- resolution de la cible ---------------------------------------------------

def _active_searcher() -> tuple[str, Searcher | None]:
    """
    Le corpus a interroger depend des mods actifs dans osu!.

    Le choix vaut pour toute la session, pas seulement pour la map courante :
    si tu joues en DT et que tu rebondis sur une reco, tu veux rester dans le
    corpus DT.
    """
    if state.searchers is None:
        return "nomod", None
    mods = state.current.mods if state.current else ""
    return state.searchers.for_mods(mods)


def _in_corpus(searcher: Searcher | None, checksum: str) -> bool:
    if not (searcher and checksum):
        return False
    row = searcher.db.execute(
        "SELECT 1 FROM maps WHERE checksum = ?", (checksum,)
    ).fetchone()
    return row is not None


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

    parser = argparse.ArgumentParser(description="Serveur local osureco")
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(create_app(args.index), host=args.host, port=args.port, log_level="warning")
    return 0
