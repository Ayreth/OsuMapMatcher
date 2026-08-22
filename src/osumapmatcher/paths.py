#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paths.py - resolution des emplacements de donnees.

Rien n'est code en dur vers un disque particulier : tout se deduit de la racine
du depot, avec des variables d'environnement pour les cas ou l'utilisateur veut
poser ses donnees ailleurs (le dump fait pres de 7 Go decompresse, on ne veut
pas forcer sa place).

    OSUMAPMATCHER_DATA      dossier de donnees (defaut : <racine>/data)
    OSUMAPMATCHER_DUMP      dossier contenant les .osu du dump ppy
    OSUMAPMATCHER_UNRANKED  dossier des .osu non classes recuperes en ligne
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

#: <racine>/src/osumapmatcher/paths.py -> <racine>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: nombre max de niveaux imbriques explores en cherchant le dump
_MAX_DEPTH = 6
#: on ne compte pas plus de .osu que ca pour departager deux niveaux
_COUNT_CAP = 5000


def data_dir() -> Path:
    override = os.environ.get("OSUMAPMATCHER_DATA")
    return Path(override).expanduser() if override else PROJECT_ROOT / "data"


def index_root() -> Path:
    return data_dir() / "index"


def corpus_root() -> Path:
    return data_dir() / "corpus"


#: nom historique du dossier des maps non classees. A eviter absolument : il
#: contient "osu_files", donc `find_dump` le prenait pour le dump lui-meme et
#: construisait le corpus principal dessus. On continue de l'accepter pour ne
#: pas obliger a deplacer plusieurs centaines de Mo deja telecharges.
_LEGACY_UNRANKED = "unranked_osu_files"


def unranked_root() -> Path:
    """
    Ou atterrissent les .osu non classes recuperes par `fetch_unranked.py`.

    Volontairement a cote du dump plutot que dedans : le dump est une archive
    officielle qu'on reextrait telle quelle, ces fichiers-la se rafraichissent
    a un autre rythme, et les melanger rendrait impossible de savoir d'ou vient
    quoi. Le manifeste vit dans ce dossier, avec les fichiers qu'il decrit.
    """
    override = os.environ.get("OSUMAPMATCHER_UNRANKED")
    if override:
        return Path(override).expanduser()
    ancien = data_dir() / _LEGACY_UNRANKED
    if ancien.is_dir():
        return ancien
    return data_dir() / "unranked_maps"


def is_unranked_dir(directory: Path) -> bool:
    """
    Ce dossier est-il un pool non classe plutot que le dump ?

    On teste la presence du manifeste ecrit par `fetch_unranked.py`, pas le nom.
    Un nom se choisit mal une fois (`unranked_osu_files` contenait `osu_files`,
    et `find_dump` construisait alors tout le corpus principal sur le mauvais
    dossier, en silence) ; un manifeste, lui, dit ce que le dossier est.
    """
    return (directory / "manifest.json").is_file()


def _count_osu(directory: Path) -> int:
    """Nombre de .osu directement dans ce dossier, plafonne."""
    try:
        return sum(1 for _ in itertools.islice(directory.glob("*.osu"), _COUNT_CAP))
    except OSError:
        return 0


def _descend(start: Path) -> tuple[Path | None, int]:
    """
    Suit les dossiers imbriques portant le meme nom et renvoie le plus fourni.

    L'archive du dump s'extrait avec plusieurs niveaux du meme nom, et les
    niveaux intermediaires peuvent contenir quelques .osu residuels. Prendre le
    premier dossier qui contient un .osu donnerait un corpus de onze fichiers
    sans que rien n'ait l'air cassé -- d'ou le choix du niveau le plus fourni.
    """
    best: Path | None = None
    best_count = 0
    current = start
    for _ in range(_MAX_DEPTH):
        count = _count_osu(current)
        if count > best_count:
            best, best_count = current, count
        nested = current / current.name
        if not nested.is_dir():
            break
        current = nested
    return best, best_count


def find_dump(hint: Path | str | None = None) -> Path | None:
    """
    Localise le dossier contenant les .osu du dump, ou None.

    Ordre : argument explicite, puis OSUMAPMATCHER_DUMP, puis les emplacements
    habituels sous le dossier de donnees et la racine du depot.
    """
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    env = os.environ.get("OSUMAPMATCHER_DUMP")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(data_dir() / "osu_files")

    for root in (data_dir(), PROJECT_ROOT):
        if not root.is_dir():
            continue
        try:
            candidates.extend(
                sorted(p for p in root.iterdir() if p.is_dir() and "osu_files" in p.name)
            )
        except OSError:
            continue

    best: Path | None = None
    best_count = 0
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        # ne jamais confondre le pool non classe avec le dump : les deux sont
        # des dossiers plats de .osu, mais construire le corpus principal sur
        # le mauvais passerait inapercu -- le comptage est plafonne, donc a
        # egalite c'est le premier vu qui gagnait, et `data/` est scanne avant
        # la racine
        if is_unranked_dir(candidate):
            continue
        found, count = _descend(candidate)
        if count > best_count:
            best, best_count = found, count
    return best


def describe_dump() -> str:
    found = find_dump()
    if found is None:
        return (
            "no .osu dump found. Download one from https://data.ppy.sh "
            "(the *_osu_files.tar.bz2 archive), extract it into "
            f"{data_dir() / 'osu_files'}, or point OSUMAPMATCHER_DUMP at it."
        )
    return f"{found} ({_count_osu(found)}+ .osu files)"
