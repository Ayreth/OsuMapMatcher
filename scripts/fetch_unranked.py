#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_unranked.py - recupere les maps non classees les plus jouees.

Elargit le pool au-dela du dump ppy, qui ne contient que du ranked et du loved.
Le classement vient d'osu.direct (le seul mirror teste qui trie par plays sans
OAuth) ; les fichiers sont tires de plusieurs mirrors en parallele, un thread
chacun, ce qui repartit la charge autant que ca accelere.

Interruptible : relancer la meme commande ne retelecharge que ce qui manque.

Usage :
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --sets 2000
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --dry-run
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --statuses graveyard
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --mirrors osu.direct,catboy

Puis, pour integrer le resultat a l'index :
    .venv\\Scripts\\python.exe scripts\\build_all.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.paths import unranked_root  # noqa: E402
from osumapmatcher.unranked import (  # noqa: E402
    DEFAULT_DELAY,
    DEFAULT_MIRROR,
    DEFAULT_POOL,
    DEFAULT_STATUSES,
    MIRRORS,
    download_refs,
    list_top_sets,
    refs_from_sets,
    resolve_mirrors,
    write_manifest,
)

#: taille moyenne d'un .osu std, mesuree sur le dump. Sert uniquement a annoncer
#: un ordre de grandeur avant de lancer un job d'une heure.
AVG_OSU_BYTES = 45_000


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Recuperation des maps non classees les plus jouees"
    )
    parser.add_argument("--sets", type=int, default=500,
                        help="nombre de beatmapsets a prendre (defaut : 500)")
    parser.add_argument("--out", type=Path, default=None,
                        help="dossier de sortie (defaut : data/unranked_osu_files)")
    parser.add_argument("--statuses", default=",".join(DEFAULT_STATUSES),
                        help="liste separee par des virgules : graveyard,pending,wip")
    parser.add_argument("--mirrors", default=",".join(DEFAULT_POOL),
                        help="sources des .osu, separees par des virgules. Un "
                             "thread par mirror : la charge se repartit et le "
                             f"debit s'additionne. Connus : {', '.join(sorted(MIRRORS))}")
    parser.add_argument("--rank-mirror", default=DEFAULT_MIRROR,
                        choices=sorted(MIRRORS),
                        help="source du classement par plays (doit savoir trier)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"pause entre requetes en s (defaut : {DEFAULT_DELAY})")
    parser.add_argument("--min-plays", type=int, default=0,
                        help="ne garder que les sets au-dessus de ce nombre de plays")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="ne pas revalider le md5 de chaque fichier (deconseille)")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="retelecharger meme ce qui est deja present")
    parser.add_argument("--dry-run", action="store_true",
                        help="classer et compter, sans rien telecharger")
    args = parser.parse_args()

    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    out_dir = args.out or unranked_root()
    rank_mirror = MIRRORS[args.rank_mirror]
    try:
        pool = resolve_mirrors([m.strip() for m in args.mirrors.split(",") if m.strip()])
    except (KeyError, ValueError) as exc:
        print(f"mirror inconnu : {exc}. Connus : {', '.join(sorted(MIRRORS))}")
        return 1

    print(f"classement : {rank_mirror.name}   "
          f"fichiers : {', '.join(m.name for m in pool)} "
          f"({len(pool)} en parallele)")
    print(f"statuts    : {', '.join(statuses)}")
    print(f"sortie     : {out_dir}")
    print()
    print(f"classement des {args.sets} sets les plus joues...")

    try:
        sets = list_top_sets(
            args.sets, statuses=statuses, mirror=rank_mirror, delay=args.delay
        )
    except ValueError as exc:
        print(f"\n{exc}")
        return 1

    if args.min_plays:
        before = len(sets)
        sets = [s for s in sets if int(s.get("play_count") or 0) >= args.min_plays]
        print(f"  seuil a {args.min_plays:,} plays : {before} -> {len(sets)} sets")

    if not sets:
        print("\naucun set retourne : le mirror a peut-etre change ses parametres.")
        return 1

    refs = refs_from_sets(sets)
    if not refs:
        print("\naucune difficulte std dans ces sets.")
        return 1

    plays = [int(s.get("play_count") or 0) for s in sets]
    print()
    print(f"{len(sets)} sets, {len(refs)} difficultes std "
          f"({len(refs) / len(sets):.1f} par set)")
    print(f"plays : de {max(plays):,} a {min(plays):,}")
    estimate = len(refs) * AVG_OSU_BYTES
    # chaque mirror tient son propre rythme, donc les debits s'additionnent
    debit = len(pool) / max(args.delay, 1e-6)
    print(f"a telecharger : ~{estimate / 1e6:.0f} Mo, "
          f"~{len(refs) / debit / 60:.0f} min a ~{debit:.0f} req/s cumules "
          f"({1 / max(args.delay, 1e-6):.0f} par mirror)")
    print()
    print("top 5 :")
    for ref in refs[:5]:
        print(f"  {ref.playcount:>12,} plays  {ref.stars:>5.2f}*  {ref.label[:58]}")
    print()

    if args.dry_run:
        print("--dry-run : rien telecharge.")
        return 0

    report = download_refs(
        refs, out_dir,
        mirrors=pool, delay=args.delay,
        verify=args.verify, resume=args.resume,
    )
    report.n_sets = len(sets)

    manifest = write_manifest(
        out_dir, refs, mirror=pool[0], statuses=statuses, report=report
    )
    print()
    print(report.render())
    print(f"manifeste : {manifest}")
    print()
    print("integre-le a l'index avec :")
    print("  .venv\\Scripts\\python.exe scripts\\build_all.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
