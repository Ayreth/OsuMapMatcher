#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sanity_check.py - le vrai point de verite du projet.

Prend une liste de beatmap_id de maps connues et affiche le top k pour chacune,
avec la decomposition de la distance par groupe de features : c'est ce qui
permet de comprendre POURQUOI une map est proposee.

Usage :
    .venv\\Scripts\\python.exe scripts\\sanity_check.py
    .venv\\Scripts\\python.exe scripts\\sanity_check.py --k 10 --star-range 0.5
    .venv\\Scripts\\python.exe scripts\\sanity_check.py --weights aim=2,speed=0.2
    .venv\\Scripts\\python.exe scripts\\sanity_check.py --ids 4646545,725718
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osureco.index import GROUP_NAMES, VARIANTS, SearcherSet  # noqa: E402
from osureco.paths import index_root  # noqa: E402

#: liste fournie par l'utilisateur. Pour le set 302535 (Acid Burst), le lien ne
#: precisait pas de difficulte : on prend la top diff, coherente avec le reste.
DEFAULT_IDS = [
    678106,   # Memme - Acid Burst [Priti & Bonzi's Ultra]
    4646545,  # TEARS OF TRAGEDY - Void Act [Memento]
    4793648,  # El Sonidito x Athletic Theme [Sotarks' MEXICAN CAT]
    1451282,  # BLUE ENCOUNT - HOPE [SURVIVOR]
    5127731,  # Aziya - bbydoll [pick me]
    967546,   # Camellia - Routing [Comfort]
    5671885,  # Pythia - Heartless [Indifference]
    725718,   # Seiryu - BLUE DRAGON [Blue Dragon]
    4880186,  # Phoneboy - Nevermind [Drunk Texts]
    4810916,  # Hanatan - Ghost Rule [Deception]
]

_SHORT = {"aim": "aim", "speed": "spd", "rhythm": "ryt",
          "spacing": "spc", "parameters": "par"}


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def parse_weights(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    weights: dict[str, float] = {}
    for chunk in raw.split(","):
        key, _, value = chunk.partition("=")
        weights[key.strip()] = float(value)
    return weights


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Controle qualite des recommandations")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--variant", default="nomod", choices=list(VARIANTS),
                        help="corpus de mods a interroger")
    parser.add_argument("--ids", default=None, help="beatmap_id separes par des virgules")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--star-range", type=float, default=0.5)
    parser.add_argument("--length-min", type=float, default=0.5)
    parser.add_argument("--length-max", type=float, default=2.0)
    parser.add_argument("--keep-same-set", action="store_true",
                        help="ne pas exclure les autres difficultes du meme set")
    parser.add_argument("--weights", default=None,
                        help="ex: aim=2,speed=0.5 (groupes: " + ", ".join(GROUP_NAMES) + ")")
    parser.add_argument("--max-per-song", type=int, default=1,
                        help="0 pour desactiver le dedoublonnage par chanson")
    parser.add_argument("--diverge", default=None, choices=list(GROUP_NAMES),
                        help="pareil mais different : proche partout sauf sur ce groupe")
    parser.add_argument("--why", action="store_true",
                        help="afficher l'explication en clair sous chaque resultat")
    args = parser.parse_args()

    ids = [int(x) for x in args.ids.split(",")] if args.ids else DEFAULT_IDS
    weights = parse_weights(args.weights)

    searcher = SearcherSet(args.data or index_root()).get(args.variant)
    print(f"index {args.variant} : {searcher.index.size} maps"
          f"   |   bande d'etoiles +/-{args.star_range}"
          f"   |   longueur x{args.length_min}-{args.length_max}"
          f"   |   meme set {'gardé' if args.keep_same_set else 'exclu'}")
    if weights:
        print(f"poids : {weights}")

    header = (f"  {'#':<3}{'dist':>6}  " + "".join(f"{_SHORT[g]:>5}" for g in GROUP_NAMES)
              + f"  {'etoiles':>8}{'bpm':>6}{'duree':>7}  map")

    for beatmap_id in ids:
        target = searcher.target_from_beatmap_id(beatmap_id)
        print()
        print("=" * 118)
        if target is None:
            print(f"id={beatmap_id} : ABSENTE du corpus")
            continue
        print(f"REQUETE  id={beatmap_id}  {target.stars:.2f}*  {target.label}")
        print(f"         BPM {target.bpm:.0f}  |  duree {mmss(target.length_s)}"
              f"  |  set {target.beatmapset_id}")
        print("-" * 118)

        started = time.perf_counter()
        matches = searcher.search(
            target,
            k=args.k,
            star_range=args.star_range,
            length_ratio=(args.length_min, args.length_max),
            exclude_same_set=not args.keep_same_set,
            weights=weights,
            max_per_song=args.max_per_song,
            diverge=args.diverge,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        if not matches:
            print("  aucun candidat apres filtrage")
            continue
        print(header)
        for m in matches:
            shares = "".join(f"{100 * m.group_share[g]:4.0f}%" for g in GROUP_NAMES)
            print(f"  {m.rank:<3}{m.distance:>6.2f}  {shares}  "
                  f"{m.stars:>7.2f}*{m.bpm:>6.0f}{mmss(m.length_s):>7}  "
                  f"{m.label} ({m.creator})")
            if args.why:
                bits = m.differences + [f"= {s}" for s in m.similarities]
                if bits:
                    print(f"      {' | '.join(bits)}")
        print(f"  -> {elapsed_ms:.0f} ms")

    searcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
