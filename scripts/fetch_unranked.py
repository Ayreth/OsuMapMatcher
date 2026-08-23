#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_unranked.py - recupere les maps non classees les plus jouees.

Elargit le pool au-dela du dump ppy, qui ne contient que du ranked et du loved.
Le classement vient d'osu.direct (le seul mirror teste qui trie par plays sans
OAuth) ; les fichiers sont tires de plusieurs mirrors en parallele, un thread
chacun, ce qui repartit la charge autant que ca accelere.

Deux facons de delimiter le lot :

  * `--sets N` prend les N sets les plus joues. C'est un top : on sait d'avance
    combien ca fait, pas ou ca s'arrete.
  * `--min-plays N` prend tout ce qui depasse N plays. C'est un pool : on sait
    ou ca s'arrete, pas combien ca fait. Les deux se cumulent, le premier
    atteint gagne.

`--min-stars` / `--max-stars` / `--max-diffs` filtrent AVANT le telechargement,
sur ce que la recherche a deja renvoye : une map ecartee ne coute pas une
requete de fichier. Les etoiles sont celles sans mods.

Interruptible : relancer la meme commande ne retelecharge que ce qui manque, et
le manifeste fusionne au lieu d'ecraser -- un lot aux nouveaux criteres ne fait
donc pas perdre les annotations d'un lot precedent.

Usage :
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --sets 500
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --dry-run --min-plays 5000
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --statuses graveyard
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py --mirrors osu.direct,catboy

    # le pool complet 5.2-11 etoiles, sets de dix difficultes au plus,
    # au-dessus de 5 000 plays
    .venv\\Scripts\\python.exe scripts\\fetch_unranked.py ^
        --min-plays 5000 --min-stars 5.2 --max-stars 11 --max-diffs 10

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
    SEARCH_DELAY,
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
    parser.add_argument("--sets", type=int, default=None,
                        help="plafond de beatmapsets. Sans --min-plays, c'est "
                             "simplement le top N (defaut : 500 si aucun des "
                             "deux n'est donne)")
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
                        help=f"pause entre deux fichiers, en s "
                             f"(defaut : {DEFAULT_DELAY})")
    parser.add_argument("--search-delay", type=float, default=SEARCH_DELAY,
                        help="pause entre deux pages de recherche, en s. La "
                             "recherche est limitee plus severement que les "
                             f"fichiers (defaut : {SEARCH_DELAY})")
    parser.add_argument("--min-plays", type=int, default=0,
                        help="descendre le classement jusqu'a ce nombre de "
                             "plays, et s'y arreter. Definit le lot au lieu de "
                             "le filtrer apres coup")
    parser.add_argument("--min-stars", type=float, default=0.0,
                        help="etoiles minimales, SANS mods (defaut : aucune "
                             "borne). En DT ces maps valent ~1.4x cette valeur")
    parser.add_argument("--max-stars", type=float, default=float("inf"),
                        help="etoiles maximales, sans mods")
    parser.add_argument("--max-diffs", type=int, default=None,
                        help="ecarter les sets de plus de N difficultes std, "
                             "borne incluse. Vise les paquets d'entrainement a "
                             "cent difficultes")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="ne pas revalider le md5 de chaque fichier (deconseille)")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="retelecharger meme ce qui est deja present")
    parser.add_argument("--dry-run", action="store_true",
                        help="classer et compter, sans rien telecharger")
    args = parser.parse_args()

    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    # aucun critere d'arret : on retombe sur l'ancien defaut plutot que de
    # partir balayer le catalogue entier parce qu'un drapeau manquait
    if args.sets is None and args.min_plays <= 0:
        args.sets = 500
    if args.min_stars > args.max_stars:
        print(f"--min-stars ({args.min_stars}) est au-dessus de --max-stars "
              f"({args.max_stars}) : aucune map ne peut passer.")
        return 1
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
    borne_haute = "aucune" if args.max_stars == float("inf") else f"{args.max_stars}"
    print(f"etoiles    : {args.min_stars} a {borne_haute} (sans mods)")
    print(f"diffs/set  : {args.max_diffs or 'sans limite'}")
    print(f"sortie     : {out_dir}")
    print()
    if args.min_plays:
        print(f"descente du classement jusqu'a {args.min_plays:,} plays"
              + (f", plafonnee a {args.sets:,} sets" if args.sets else "")
              + "...")
    else:
        print(f"classement des {args.sets} sets les plus joues...")

    try:
        sets = list_top_sets(
            args.sets,
            min_playcount=args.min_plays,
            statuses=statuses,
            mirror=rank_mirror,
            delay=args.search_delay,
        )
    except ValueError as exc:
        print(f"\n{exc}")
        return 1

    if not sets:
        print("\naucun set retourne : le mirror a peut-etre change ses parametres.")
        return 1

    refs = refs_from_sets(
        sets,
        min_stars=args.min_stars,
        max_stars=args.max_stars,
        max_diffs=args.max_diffs,
        min_playcount=args.min_plays,
    )
    if not refs:
        print("\naucune difficulte std ne passe les filtres.")
        return 1

    # ce qui est deja sur le disque ne sera pas retelecharge : c'est ce chiffre
    # qui dit le vrai cout du lot, pas le nombre de difficultes retenues
    manquantes = sum(1 for r in refs if not (out_dir / r.filename).is_file())

    plays = [int(s.get("play_count") or 0) for s in sets]
    etoiles = [r.stars for r in refs]
    print()
    print(f"{len(sets)} sets, {len(refs)} difficultes std retenues "
          f"({len(refs) / len(sets):.2f} par set)")
    print(f"plays   : de {max(plays):,} a {min(plays):,}")
    print(f"etoiles : de {min(etoiles):.2f} a {max(etoiles):.2f}")
    print(f"deja sur le disque : {len(refs) - manquantes} / {len(refs)}")
    estimate = manquantes * AVG_OSU_BYTES
    # chaque mirror tient son propre rythme, donc les debits s'additionnent
    debit = len(pool) / max(args.delay, 1e-6)
    print(f"a telecharger : {manquantes} maps, ~{estimate / 1e6:.0f} Mo au plus, "
          f"~{manquantes / debit / 60:.0f} min a ~{debit:.0f} req/s cumules "
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
        out_dir, refs, mirror=pool[0], statuses=statuses, report=report,
        criteria={
            "min_plays": args.min_plays,
            "min_stars": args.min_stars,
            "max_stars": None if args.max_stars == float("inf") else args.max_stars,
            "max_diffs": args.max_diffs,
            "sets_cap": args.sets,
        },
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
