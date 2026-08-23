#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish_index.py - pousse un corpus frais vers le serveur et rebatit l'index.

A lancer depuis la machine de BUILD, celle qui a le dump et les .osu non
classes. Le serveur, lui, n'a ni l'un ni l'autre : il ne recoit que des
features deja calculees.

Ce qui monte, et pourquoi si peu :

    data/corpus/<variante>_unranked/   les seules tranches qui bougent quand on
                                       elargit le pool -- ~2,2 ko par map, soit
                                       une centaine de Mo pour 40 000 maps
    <pool>/manifest.json               statut et playcount de chaque map : la
                                       seule source des colonnes `status` et
                                       `playcount` de l'index
    data/loved.json                    ce qui separe `ranked` de `loved`

Les corpus du dump (`data/corpus/<variante>`, 360 Mo) ne montent qu'avec
`--full` : le dump ne change que quand ppy en publie un nouveau, et refaire
360 Mo d'upload pour rien a chaque elargissement du pool n'a pas de sens.

Le serveur enchaine ensuite `deploy/update-index.sh`, qui construit a cote de
l'index en service et ne bascule qu'apres l'avoir vu repondre.

Usage :
    .venv\\Scripts\\python.exe scripts\\publish_index.py --dry-run
    .venv\\Scripts\\python.exe scripts\\publish_index.py
    .venv\\Scripts\\python.exe scripts\\publish_index.py --variants nomod,DT
    .venv\\Scripts\\python.exe scripts\\publish_index.py --full
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.index import VARIANTS  # noqa: E402
from osumapmatcher.paths import corpus_root, data_dir, unranked_root  # noqa: E402
from osumapmatcher.unranked import LOVED_FILE, MANIFEST_FILE  # noqa: E402

#: le deploiement d'Ayreth. Surchargeable, mais c'est le seul qui existe.
DEFAULT_HOST = "ubuntu@51.255.74.207"
DEFAULT_REMOTE = "/home/ubuntu/Claude/osumapmatcher"

#: le serveur nomme son pool `data/unranked`, la machine de build garde encore
#: le nom historique `data/unranked_osu_files`. On ne renomme rien ici : des
#: centaines de Mo de .osu d'un cote, un chemin ecrit dans DEPLOIEMENT.md de
#: l'autre. On traduit au moment de copier, c'est tout.
REMOTE_UNRANKED = "data/unranked"


def run(cmd: list[str], *, dry: bool, **kwargs) -> subprocess.CompletedProcess | None:
    joli = " ".join(str(c) for c in cmd)
    print(f"  $ {joli[:150]}{'...' if len(joli) > 150 else ''}", flush=True)
    if dry:
        return None
    return subprocess.run(cmd, check=True, **kwargs)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Publie le corpus sur le serveur et y rebatit l'index"
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"cible ssh (defaut : {DEFAULT_HOST})")
    parser.add_argument("--remote", default=DEFAULT_REMOTE,
                        help=f"depot sur le serveur (defaut : {DEFAULT_REMOTE})")
    parser.add_argument("--variants", default=",".join(VARIANTS),
                        help="variantes a publier, separees par des virgules")
    parser.add_argument("--full", action="store_true",
                        help="envoyer aussi les corpus du dump (~360 Mo). "
                             "Necessaire seulement apres un nouveau dump ppy")
    parser.add_argument("--no-pull", dest="pull", action="store_false",
                        help="ne pas mettre le code du serveur a jour")
    parser.add_argument("--no-build", dest="build", action="store_false",
                        help="envoyer les fichiers sans rebatir l'index")
    parser.add_argument("--dry-run", action="store_true",
                        help="montrer les commandes sans rien executer")
    args = parser.parse_args()

    dry = args.dry_run
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    racine = Path(__file__).resolve().parents[1]
    corpus = corpus_root()
    pool = unranked_root()

    # --- ce qu'on a sous la main -------------------------------------------
    a_envoyer: list[str] = []
    for variant in variants:
        for nom in ([variant, f"{variant}_unranked"] if args.full
                    else [f"{variant}_unranked"]):
            chemin = corpus / nom
            if not chemin.is_dir():
                print(f"corpus absent : {chemin}")
                print("  lance d'abord scripts\\build_all.py")
                return 1
            a_envoyer.append(nom)

    manifest = pool / MANIFEST_FILE
    loved = data_dir() / LOVED_FILE
    for fichier in (manifest, loved):
        if not fichier.is_file():
            print(f"fichier absent : {fichier}")
            return 1

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    n_maps = payload.get("n_total") or len(payload.get("beatmaps") or {})
    poids = sum(
        f.stat().st_size
        for nom in a_envoyer
        for f in (corpus / nom).rglob("*")
        if f.is_file()
    )

    print(f"serveur   : {args.host}:{args.remote}")
    print(f"variantes : {', '.join(variants)}")
    print(f"corpus    : {len(a_envoyer)} dossiers, {poids / 1e6:.0f} Mo avant "
          f"compression")
    print(f"manifeste : {n_maps} maps, du {payload.get('fetched_at', '?')}")
    print(f"            criteres : {payload.get('criteria') or 'non enregistres'}")
    print()

    ssh = ["ssh", args.host]

    # --- code -----------------------------------------------------------------
    if args.pull:
        print("# code du serveur")
        run(ssh + [f"cd {args.remote} && git pull --ff-only"], dry=dry)
        print()

    # --- corpus ---------------------------------------------------------------
    # tar traverse le tuyau ssh : pas de rsync sous Git Bash, et un tar
    # compresse fait mieux que scp sur des parquet de toute facon
    print("# corpus")
    tar = ["tar", "-czf", "-", "-C", str(corpus)] + a_envoyer
    distant = f"mkdir -p {args.remote}/data/corpus && tar -xzf - -C {args.remote}/data/corpus"
    print(f"  $ {' '.join(tar[:6])} ... | ssh {args.host} 'tar -xzf - ...'")
    if not dry:
        emetteur = subprocess.Popen(tar, stdout=subprocess.PIPE)
        recepteur = subprocess.Popen(ssh + [distant], stdin=emetteur.stdout)
        emetteur.stdout.close()
        recepteur.communicate()
        emetteur.wait()
        if emetteur.returncode or recepteur.returncode:
            print(f"\ntransfert echoue (tar={emetteur.returncode}, "
                  f"ssh={recepteur.returncode}). Rien n'a ete rebati.")
            return 1
    print()

    # --- annotations ----------------------------------------------------------
    print("# manifeste et loved")
    run(ssh + [f"mkdir -p {args.remote}/{REMOTE_UNRANKED}"], dry=dry)
    run(["scp", "-q", str(manifest),
         f"{args.host}:{args.remote}/{REMOTE_UNRANKED}/{MANIFEST_FILE}"], dry=dry)
    run(["scp", "-q", str(loved),
         f"{args.host}:{args.remote}/data/{LOVED_FILE}"], dry=dry)
    print()

    if not args.build:
        print("--no-build : fichiers en place, index inchange. Pour le rebatir :")
        print(f"  ssh {args.host} '{args.remote}/deploy/update-index.sh'")
        return 0

    # --- reconstruction -------------------------------------------------------
    print("# reconstruction de l'index sur le serveur")
    print()
    if dry:
        print(f"  $ ssh {args.host} 'cd {args.remote} && "
              f"./deploy/update-index.sh {' '.join(variants)}'")
        print()
        print("--dry-run : rien envoye, rien rebati.")
        return 0

    resultat = subprocess.run(
        ssh + [f"cd {args.remote} && chmod +x deploy/update-index.sh && "
               f"./deploy/update-index.sh {' '.join(variants)}"]
    )
    if resultat.returncode:
        print()
        print("la reconstruction a echoue. L'API sert toujours l'index precedent "
              "-- update-index.sh ne bascule qu'apres une reponse valide.")
        return resultat.returncode

    print()
    print("publie. Verifie ce que voit un client :")
    print("  curl -A OsuMapMatcher https://api.ayreth.site/health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
