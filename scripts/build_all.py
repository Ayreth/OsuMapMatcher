#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_all.py - construit corpus + index pour chaque combinaison de mods.

Une map jouee en DT ne se compare pas a un corpus nomod : accelerée elle vaut
douze etoiles, et on irait chercher des maps a douze etoiles sans mods, dont il
n'existe presque rien. Chaque variante a donc son propre corpus et son propre
index, batis avec exactement le meme code.

Usage :
    .venv\\Scripts\\python.exe scripts\\build_all.py
    .venv\\Scripts\\python.exe scripts\\build_all.py --variants nomod,DT
    .venv\\Scripts\\python.exe scripts\\build_all.py --no-resume
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.corpus import build_corpus  # noqa: E402
from osumapmatcher.index import VARIANTS, build  # noqa: E402
from osumapmatcher.paths import corpus_root, describe_dump, find_dump, index_root  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Construction complete (corpus + index)")
    parser.add_argument("--source", type=Path, default=None,
                        help="folder holding the .osu dump (auto-detected if omitted)")
    parser.add_argument("--corpus-root", type=Path, default=None)
    parser.add_argument("--index-root", type=Path, default=None)
    parser.add_argument("--variants", default=",".join(VARIANTS),
                        help="liste separee par des virgules, ex: nomod,DT")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    args = parser.parse_args()

    source = args.source or find_dump()
    if source is None or not source.exists():
        print(describe_dump())
        return 1
    corpus_dir_root = args.corpus_root or corpus_root()
    index_dir_root = args.index_root or index_root()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    started = time.perf_counter()
    summary: list[tuple[str, int, float]] = []

    for position, variant in enumerate(variants, start=1):
        mods = None if variant == "nomod" else variant
        print()
        print("#" * 78)
        print(f"# VARIANTE {position}/{len(variants)} : {variant}")
        print("#" * 78)

        corpus_dir = corpus_dir_root / variant
        report = build_corpus(
            source, corpus_dir,
            workers=args.workers, resume=args.resume, mods=mods,
        )
        print()
        print(report.render())

        index_dir = index_dir_root / variant
        stats = build(corpus_dir, index_dir)
        print(f"index {variant} : {stats['n_maps']} maps, "
              f"{stats['vectors_mb'] + stats['index_mb'] + stats['sqlite_mb']:.0f} Mo"
              f" -> {index_dir}")
        summary.append((variant, stats["n_maps"], report.elapsed_s))

    print()
    print("=" * 78)
    print("TOUTES LES VARIANTES")
    print("=" * 78)
    for variant, n_maps, elapsed in summary:
        print(f"  {variant:<8} {n_maps:>8} maps   ({elapsed:.0f} s de featurisation)")
    print(f"  total : {(time.perf_counter() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
