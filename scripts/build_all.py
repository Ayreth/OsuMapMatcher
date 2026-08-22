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
from osumapmatcher.paths import (  # noqa: E402
    corpus_root,
    describe_dump,
    find_dump,
    index_root,
    unranked_root,
)
from osumapmatcher.unranked import load_annotations  # noqa: E402


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
    parser.add_argument("--unranked", type=Path, default=None,
                        help="dossier de .osu non classes (defaut : data/unranked_osu_files)")
    parser.add_argument("--no-unranked", dest="use_unranked", action="store_false",
                        help="ignorer les maps non classees meme si elles sont la")
    args = parser.parse_args()

    source = args.source or find_dump()
    if source is None or not source.exists():
        print(describe_dump())
        return 1
    corpus_dir_root = args.corpus_root or corpus_root()
    index_dir_root = args.index_root or index_root()

    # les maps non classees sont optionnelles : sans elles, tout se comporte
    # exactement comme avant. C'est le dossier qui decide, pas un drapeau a
    # retenir -- si `fetch_unranked.py` est passe par la, elles entrent.
    unranked_dir = args.unranked or unranked_root()
    annotations: dict = {}
    if args.use_unranked and unranked_dir.is_dir():
        annotations = load_annotations(unranked_dir)
    if annotations:
        print(f"maps non classees : {len(annotations)} depuis {unranked_dir}")
    else:
        unranked_dir = None
        print("maps non classees : aucune "
              "(scripts/fetch_unranked.py pour elargir le pool)")

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

        # corpus separe pour les maps non classees : elles se rafraichissent a
        # un autre rythme que le dump, et la reprise par tranches est indexee
        # par position -- les melanger la casserait a chaque ajout
        sources = [corpus_dir]
        if unranked_dir is not None:
            extra_dir = corpus_dir_root / f"{variant}_unranked"
            print()
            extra = build_corpus(
                unranked_dir, extra_dir,
                workers=args.workers, resume=args.resume, mods=mods,
            )
            print(f"non classees {variant} : {extra.n_kept} maps retenues "
                  f"sur {extra.n_seen}")
            sources.append(extra_dir)

        index_dir = index_dir_root / variant
        stats = build(sources, index_dir, annotations=annotations)
        print(f"index {variant} : {stats['n_maps']} maps "
              f"(dont {stats['n_unranked']} non classees), "
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
