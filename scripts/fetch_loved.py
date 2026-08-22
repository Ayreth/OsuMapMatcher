#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_loved.py - recupere les md5 des maps loved.

Le dump ppy contient les maps ranked ET loved, sans jamais dire lesquelles sont
lesquelles. Sans cette liste, l'index les regroupe donc sous un statut `dump`
volontairement vague, parce qu'ecrire `ranked` partout serait faux pour
plusieurs milliers de maps.

Ce script ne telecharge aucun fichier : ces maps sont deja dans le dump. Il ne
va chercher que leurs checksums, pour pouvoir etiqueter des lignes qui
existent deja. Environ 1 300 sets, quatorze requetes, cinq secondes.

Usage :
    .venv\\Scripts\\python.exe scripts\\fetch_loved.py

Puis, pour que l'index en tienne compte :
    .venv\\Scripts\\python.exe scripts\\build_all.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.paths import data_dir  # noqa: E402
from osumapmatcher.unranked import (  # noqa: E402
    DEFAULT_DELAY,
    DEFAULT_MIRROR,
    LOVED_FILE,
    MIRRORS,
    list_loved_checksums,
    write_loved,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Recuperation des md5 des maps loved (aucun telechargement)"
    )
    parser.add_argument("--out", type=Path, default=None,
                        help=f"fichier de sortie (defaut : data/{LOVED_FILE})")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, choices=sorted(MIRRORS),
                        help="source de la recherche (doit savoir filtrer par statut)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args()

    out = args.out or (data_dir() / LOVED_FILE)
    mirror = MIRRORS[args.mirror]
    print(f"source : {mirror.name}")
    print(f"sortie : {out}")
    print()

    try:
        checksums = list_loved_checksums(mirror=mirror, delay=args.delay)
    except ValueError as exc:
        print(f"\n{exc}")
        return 1

    if not checksums:
        print("\naucun md5 recupere : le mirror a peut-etre change ses parametres.")
        return 1

    write_loved(out, checksums)
    print()
    print(f"{len(checksums)} difficultes loved.")
    print()
    print("prends-les en compte avec :")
    print("  .venv\\Scripts\\python.exe scripts\\build_all.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
