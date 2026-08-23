# -*- coding: utf-8 -*-
"""
Tests du decoupage d'un pool : seuil de plays, fenetre d'etoiles, densite des
sets, et fusion du manifeste.

Ce qui est reellement en jeu ici n'est pas le filtrage -- trois comparaisons --
mais deux proprietes qui ne se voient pas a la lecture :

  * un seuil de plays doit ARRETER la pagination, pas seulement jeter des lignes
    apres coup. Sur 36 000 sets, la difference est de trois cent soixante
    requetes contre deux mille ;
  * un lot aux nouveaux criteres ne doit pas faire disparaitre du manifeste les
    maps d'un lot precedent. Leurs `.osu` restent sur le disque ; sans leur
    annotation, `build_index` les prend pour des maps du dump et etiquette
    `ranked` des maps graveyard, sans rien signaler.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from osumapmatcher import unranked as unranked_module
from osumapmatcher.unranked import (
    MANIFEST_FILE,
    MIRRORS,
    BeatmapRef,
    FetchReport,
    list_top_sets,
    load_annotations,
    refs_from_sets,
    write_manifest,
)


def _beatmap(bid: int, stars: float = 5.5, mode: int = 0) -> dict:
    return {
        "id": bid,
        "mode_int": mode,
        "checksum": f"{bid:032x}",
        "version": f"Diff {bid}",
        "difficulty_rating": stars,
    }


def _set(set_id: int, plays: int, beatmaps: list[dict]) -> dict:
    return {
        "id": set_id,
        "play_count": plays,
        "status": "graveyard",
        "artist": f"Artist {set_id}",
        "title": f"Title {set_id}",
        "beatmaps": beatmaps,
    }


# --- fenetre d'etoiles --------------------------------------------------------

def test_la_fenetre_d_etoiles_est_inclusive_aux_deux_bouts():
    sets = [_set(1, 9000, [
        _beatmap(10, stars=5.19),
        _beatmap(11, stars=5.20),
        _beatmap(12, stars=8.00),
        _beatmap(13, stars=11.00),
        _beatmap(14, stars=11.01),
    ])]
    gardes = {r.beatmap_id for r in refs_from_sets(sets, min_stars=5.2, max_stars=11.0)}

    assert gardes == {11, 12, 13}, "les bornes elles-memes passent, ce qui deborde non"


def test_sans_fenetre_tout_passe():
    sets = [_set(1, 9000, [_beatmap(10, stars=0.5), _beatmap(11, stars=15.0)])]

    assert len(refs_from_sets(sets)) == 2


def test_les_etoiles_retenues_sont_celles_annoncees():
    sets = [_set(1, 9000, [_beatmap(10, stars=7.25)])]

    assert refs_from_sets(sets, min_stars=5.2)[0].stars == pytest.approx(7.25)


# --- densite des sets ---------------------------------------------------------

def test_max_diffs_ecarte_le_set_entier_pas_seulement_le_surplus():
    dense = _set(1, 9000, [_beatmap(100 + i) for i in range(11)])
    mince = _set(2, 8000, [_beatmap(200 + i) for i in range(3)])
    refs = refs_from_sets([dense, mince], max_diffs=10)

    assert {r.beatmapset_id for r in refs} == {2}, (
        "un set trop dense part en entier : garder ses dix premieres "
        "difficultes reviendrait a garder justement ce qu'on voulait ecarter"
    )


def test_max_diffs_est_une_borne_incluse():
    pile = _set(1, 9000, [_beatmap(100 + i) for i in range(10)])

    assert len(refs_from_sets([pile], max_diffs=10)) == 10
    assert refs_from_sets([pile], max_diffs=9) == []


def test_max_diffs_ne_compte_que_le_std():
    # trois std noyees dans cinquante mania : c'est un set de trois difficultes
    mania = [_beatmap(500 + i, mode=3) for i in range(50)]
    std = [_beatmap(600 + i) for i in range(3)]
    refs = refs_from_sets([_set(1, 9000, mania + std)], max_diffs=10)

    assert {r.beatmap_id for r in refs} == {600, 601, 602}


# --- seuil de plays -----------------------------------------------------------

def test_le_seuil_de_plays_ecarte_les_sets_trop_peu_joues():
    sets = [_set(1, 9000, [_beatmap(10)]), _set(2, 4999, [_beatmap(11)])]

    assert [r.beatmap_id for r in refs_from_sets(sets, min_playcount=5000)] == [10]


class _Recherche:
    """Un catalogue paginable, qui compte les pages qu'on lui demande."""

    def __init__(self, plays: list[int]):
        self.sets = [
            _set(i, p, [_beatmap(1000 + i)]) for i, p in enumerate(plays)
        ]
        self.pages: list[int] = []

    def __call__(self, url: str, *, timeout: float = 30.0) -> bytes:
        params = dict(
            morceau.split("=", 1)
            for morceau in url.split("?", 1)[1].split("&")
        )
        offset, amount = int(params["offset"]), int(params["amount"])
        self.pages.append(offset)
        return json.dumps(self.sets[offset:offset + amount]).encode()


@pytest.fixture
def catalogue(monkeypatch):
    def installer(plays: list[int]) -> _Recherche:
        recherche = _Recherche(plays)
        monkeypatch.setattr(unranked_module, "_get", recherche)
        monkeypatch.setattr(unranked_module.time, "sleep", lambda _s: None)
        monkeypatch.setattr(unranked_module, "PAGE_SIZE", 10)
        return recherche

    return installer


def test_le_seuil_arrete_la_pagination(catalogue):
    # 1 000 sets decroissants de 10 en 10, pages de 10 : le seuil de 9 750
    # tombe dans la troisieme page (9 800 -> 9 710)
    recherche = catalogue(list(range(10_000, 0, -10)))
    sets = list_top_sets(
        min_playcount=9_750, statuses=["graveyard"],
        mirror=MIRRORS["osu.direct"], progress=False,
    )

    assert [s["play_count"] for s in sets] == list(range(10_000, 9_740, -10))
    assert recherche.pages == [0, 10, 20], (
        "on va jusqu'a la page qui franchit le seuil, et pas plus loin : "
        "le tri est decroissant, la suite est forcement sous le seuil"
    )


def test_le_seuil_ne_scanne_pas_tout_le_catalogue(catalogue):
    recherche = catalogue(list(range(100_000, 0, -100)))
    list_top_sets(
        min_playcount=99_000, statuses=["graveyard"],
        mirror=MIRRORS["osu.direct"], progress=False,
    )

    assert len(recherche.pages) == 2, "deux pages, pas cent"


def test_un_plafond_de_sets_borne_le_seuil(catalogue):
    catalogue(list(range(10_000, 0, -1)))
    sets = list_top_sets(
        25, min_playcount=1, statuses=["graveyard"],
        mirror=MIRRORS["osu.direct"], progress=False,
    )

    assert len(sets) == 25, "le premier critere atteint gagne"


def test_sans_critere_d_arret_c_est_une_erreur_claire():
    with pytest.raises(ValueError, match="critere d'arret"):
        list_top_sets(mirror=MIRRORS["osu.direct"], progress=False)


def test_le_top_sans_seuil_marche_comme_avant(catalogue):
    catalogue(list(range(10_000, 0, -100)))
    sets = list_top_sets(
        30, statuses=["graveyard"], mirror=MIRRORS["osu.direct"], progress=False,
    )

    assert len(sets) == 30


# --- fusion du manifeste ------------------------------------------------------

def _ref(bid: int, **extra) -> BeatmapRef:
    fields = {
        "beatmap_id": bid, "beatmapset_id": 1,
        "checksum": hashlib.md5(str(bid).encode()).hexdigest(),
        "status": "graveyard", "playcount": 1000, "stars": 5.0,
        "label": f"map {bid}",
    }
    fields.update(extra)
    return BeatmapRef(**fields)


def _ecrire_lot(dossier, refs):
    """Simule un passage : les .osu sur le disque, et le manifeste qui va avec."""
    for ref in refs:
        (dossier / ref.filename).write_bytes(str(ref.beatmap_id).encode())
    return write_manifest(
        dossier, refs, mirror=MIRRORS["osu.direct"],
        statuses=["graveyard"], report=FetchReport(),
    )


def test_un_second_lot_ne_perd_pas_les_annotations_du_premier(tmp_path):
    _ecrire_lot(tmp_path, [_ref(10), _ref(11)])
    _ecrire_lot(tmp_path, [_ref(12)])

    annotations = load_annotations(tmp_path)
    assert {a["beatmap_id"] for a in annotations.values()} == {10, 11, 12}, (
        "les .osu du premier lot sont encore la : sans leur annotation, "
        "build_index les prendrait pour des maps du dump"
    )


def test_la_fusion_oublie_ce_qui_n_est_plus_sur_le_disque(tmp_path):
    _ecrire_lot(tmp_path, [_ref(10), _ref(11)])
    (tmp_path / "11.osu").unlink()
    _ecrire_lot(tmp_path, [_ref(12)])

    annotations = load_annotations(tmp_path)
    assert {a["beatmap_id"] for a in annotations.values()} == {10, 12}, (
        "un manifeste ne decrit que ce que le dossier contient vraiment"
    )


def test_le_second_lot_gagne_sur_une_map_commune(tmp_path):
    _ecrire_lot(tmp_path, [_ref(10, playcount=1000, status="pending")])
    _ecrire_lot(tmp_path, [_ref(10, playcount=7777, status="graveyard")])

    entree = next(iter(load_annotations(tmp_path).values()))
    assert (entree["playcount"], entree["status"]) == (7777, "graveyard"), (
        "un playcount se rafraichit ; c'est la mesure la plus recente qui vaut"
    )


def test_merge_false_repart_de_zero(tmp_path):
    _ecrire_lot(tmp_path, [_ref(10)])
    write_manifest(
        tmp_path, [_ref(12)], mirror=MIRRORS["osu.direct"],
        statuses=["graveyard"], report=FetchReport(), merge=False,
    )

    assert {a["beatmap_id"] for a in load_annotations(tmp_path).values()} == {12}


def test_le_manifeste_garde_la_trace_des_criteres(tmp_path):
    write_manifest(
        tmp_path, [_ref(10)], mirror=MIRRORS["osu.direct"],
        statuses=["graveyard"], report=FetchReport(),
        criteria={"min_plays": 5000, "min_stars": 5.2, "max_stars": 11.0},
    )
    payload = json.loads((tmp_path / MANIFEST_FILE).read_text(encoding="utf-8"))

    assert payload["criteria"]["min_stars"] == 5.2, (
        "relire un pool six mois plus tard demande de savoir comment il a ete "
        "decoupe : le nombre de maps seul ne le dit pas"
    )
