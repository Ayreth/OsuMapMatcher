#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_corpus.py - phase 3. Featurise tout le dump vers des parquets par tranches.

Interruptible : relancer la meme commande reprend a la tranche suivante.

Usage :
    .venv\\Scripts\\python.exe scripts\\build_corpus.py
    .venv\\Scripts\\python.exe scripts\\build_corpus.py --limit 5000   # essai
    .venv\\Scripts\\python.exe scripts\\build_corpus.py --no-resume    # tout refaire
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.corpus import DEFAULT_CHUNK_SIZE, build_corpus  # noqa: E402
from osumapmatcher.paths import corpus_root, describe_dump, find_dump  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Construction du corpus (phase 3)")
    parser.add_argument("--source", type=Path, default=None,
                        help="dossier de .osu ou archive .tar.bz2 (auto-detecte sinon)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--limit", type=int, default=None,
                        help="ne traiter que les N premiers fichiers")
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--mods", default=None,
                        help="featuriser tout le dump avec ces mods, ex: DT, DTHR")
    args = parser.parse_args()

    source = args.source or find_dump()
    if source is None or not source.exists():
        print(describe_dump())
        return 1

    report = build_corpus(
        source,
        args.out or corpus_root(),
        workers=args.workers,
        chunk_size=args.chunk_size,
        limit=args.limit,
        resume=args.resume,
        mods=args.mods,
    )
    print()
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
