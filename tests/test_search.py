# -*- coding: utf-8 -*-
"""
Tests de la recherche elle-meme (index.py), sur un index minuscule.

Deux regressions payees cher sont gardees ici :

  * les vecteurs sur disque sont colonne-major (`DataFrame.to_numpy()` les rend
    ainsi), donc une ligne prise telle quelle n'est pas contigue et usearch la
    refuse. Ca ne se declenchait que sur une bande d'etoiles dense, c'est-a-dire
    exactement sur les maps que tout le monde joue ;
  * le pre-filtrage est passe de SQL a numpy pour des raisons de temps. Le test
    compare les deux implementations plutot que de faire confiance a la lecture.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from osumapmatcher import index as index_module
from osumapmatcher.index import Searcher


@pytest.fixture(scope="module")
def searcher(mini_index) -> Searcher:
    found = Searcher(mini_index / "nomod")
    yield found
    found.close()


def _sql_candidates(searcher: Searcher, target, star_range, length_ratio,
                    exclude_same_set) -> np.ndarray:
    """L'ancien pre-filtrage, garde comme reference."""
    clauses = ["stars BETWEEN ? AND ?"]
    params: list = [target.stars - star_range, target.stars + star_range]
    if length_ratio and target.length_s > 0:
        clauses.append("length_s BETWEEN ? AND ?")
        params += [target.length_s * length_ratio[0],
                   target.length_s * length_ratio[1]]
    if exclude_same_set and target.beatmapset_id:
        clauses.append("beatmapset_id != ?")
        params.append(target.beatmapset_id)
    if target.checksum:
        clauses.append("checksum != ?")
        params.append(target.checksum)
    rows = searcher.db.execute(
        "SELECT pos FROM maps WHERE " + " AND ".join(clauses), params
    ).fetchall()
    return np.sort(np.fromiter((row["pos"] for row in rows), dtype=np.int64,
                               count=len(rows)))


def _checksums(searcher: Searcher) -> list[str]:
    return [row["checksum"] for row in
            searcher.db.execute("SELECT checksum FROM maps ORDER BY pos")]


def test_les_vecteurs_sur_disque_sont_bien_colonne_major(searcher):
    """Sans ca, le test suivant ne testerait rien."""
    assert searcher.vectors.flags["F_CONTIGUOUS"]
    assert not searcher.vectors.flags["C_CONTIGUOUS"]


def test_la_cible_lue_dans_le_corpus_est_contigue(searcher):
    target = searcher.target_from_checksum(_checksums(searcher)[0])
    assert target is not None
    assert target.vector.flags["C_CONTIGUOUS"]
    assert target.raw.flags["C_CONTIGUOUS"]


def test_le_chemin_ann_accepte_un_index_colonne_major(searcher, monkeypatch):
    """
    Regression : au-dela de EXACT_LIMIT candidats la recherche passe par
    usearch, qui refuse une ligne non contigue. Le seuil est abaisse a zero
    pour que le petit corpus emprunte quand meme ce chemin.
    """
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    target = searcher.target_from_checksum(_checksums(searcher)[0])
    matches = searcher.search(target, k=3, star_range=10.0)
    assert matches, "le chemin ANN doit rendre des resultats"
    assert all(match.checksum != target.checksum for match in matches)


@pytest.mark.parametrize("star_range,ratio,same_set",
                         [(10.0, None, False), (0.5, (0.5, 2.0), True),
                          (2.0, (0.8, 1.25), True)])
def test_le_prefiltre_numpy_donne_le_meme_ensemble_que_le_filtre_sql(
    searcher, star_range, ratio, same_set
):
    for checksum in _checksums(searcher):
        target = searcher.target_from_checksum(checksum)
        expected = _sql_candidates(searcher, target, star_range, ratio, same_set)
        got = np.sort(searcher._candidates(
            target, star_range=star_range, length_ratio=ratio,
            exclude_same_set=same_set,
        ))
        assert np.array_equal(expected, got), checksum


def test_la_map_interrogee_ne_se_recommande_pas_elle_meme(searcher):
    for checksum in _checksums(searcher):
        target = searcher.target_from_checksum(checksum)
        candidates = searcher._candidates(
            target, star_range=100.0, length_ratio=None, exclude_same_set=False
        )
        assert searcher._position_of(checksum) not in candidates.tolist()


def test_une_position_absente_de_la_table_n_est_jamais_candidate(mini_index, tmp_path):
    """
    Les positions viennent de la table ; un trou dedans ne doit pas fabriquer
    une map fantome a zero etoile, qui serait un voisin parfait de rien.
    """
    import shutil

    copy = tmp_path / "nomod"
    shutil.copytree(mini_index / "nomod", copy)
    connection = sqlite3.connect(copy / "corpus.sqlite")
    connection.execute("DELETE FROM maps WHERE pos = 1")
    connection.commit()
    connection.close()

    searcher = Searcher(copy)
    try:
        target = searcher.target_from_checksum(_checksums(searcher)[0])
        candidates = searcher._candidates(
            target, star_range=100.0, length_ratio=None, exclude_same_set=False
        )
        assert 1 not in candidates.tolist()
    finally:
        searcher.close()


def test_le_chemin_ann_accepte_une_cible_non_contigue(searcher, monkeypatch):
    """
    L'appelant peut fabriquer son vecteur autrement (l'API distante le fait a
    partir d'un dict envoye par le client) : la recherche ne doit pas dependre
    de la disposition memoire de ce qu'on lui donne.
    """
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    target = searcher.target_from_checksum(_checksums(searcher)[0])
    # une ligne d'un tableau colonne-major : memes valeurs, autre disposition
    target.vector = np.asfortranarray(np.tile(target.vector, (2, 1)))[0]
    assert not target.vector.flags["C_CONTIGUOUS"]
    assert searcher.search(target, k=3, star_range=10.0)


def test_le_searcher_supporte_des_recherches_simultanees(searcher):
    """
    Regression : la connexion sqlite est partagee et ouverte en
    check_same_thread=False. Sans verrou, deux threads se volent le curseur et
    `row["pos"]` finit par lever `IndexError: tuple index out of range` --
    observe en vrai des qu'un joueur enchainait vite les maps, et servi au
    navigateur en 500.
    """
    import concurrent.futures

    checksums = _checksums(searcher)
    inconnus = ["%032x" % (i * 7919) for i in range(20)]

    def travail(i: int):
        checksum = checksums[i % len(checksums)]
        # on melange les trois acces SQL de la classe, comme le fait une page
        # qui redemande pendant qu'une autre requete est encore en vol
        searcher.has_checksum(inconnus[i % len(inconnus)])
        target = searcher.target_from_checksum(checksum)
        assert target is not None
        return len(searcher.search(target, k=3, star_range=10.0))

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        resultats = list(pool.map(travail, range(120)))
    assert all(count > 0 for count in resultats)
