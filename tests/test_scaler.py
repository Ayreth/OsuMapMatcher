# -*- coding: utf-8 -*-
"""
Tests du scaler : round-trip disque, et equivalence avec sklearn.

Le scaler est persiste sous forme de tableaux (centre, echelle) et non picklé.
Ces tests verifient que cette forme reproduit bien RobustScaler a l'identique et
survit a un aller-retour sur disque, puisque c'est exactement le meme objet qui
doit servir cote corpus et cote map live.
"""

from __future__ import annotations

import numpy as np
import pytest

from osumapmatcher.features import FEATURE_NAMES, GROUP_NAMES
from osumapmatcher.index import Scaler, _weight_vector, normalise_weights


@pytest.fixture
def matrix() -> np.ndarray:
    rng = np.random.default_rng(20260821)
    data = rng.normal(size=(500, len(FEATURE_NAMES))) * rng.uniform(
        0.1, 1000.0, size=len(FEATURE_NAMES)
    )
    # quelques valeurs aberrantes, comme dans le vrai corpus
    data[0] *= 5000.0
    return data


def test_round_trip_disque(matrix, tmp_path):
    scaler = Scaler.fit(matrix)
    path = tmp_path / "scaler.npz"
    scaler.save(path)
    reloaded = Scaler.load(path)

    assert np.array_equal(scaler.center, reloaded.center)
    assert np.array_equal(scaler.scale, reloaded.scale)
    assert scaler.clip == reloaded.clip
    assert reloaded.feature_names == tuple(FEATURE_NAMES)
    assert np.array_equal(scaler.transform(matrix), reloaded.transform(matrix))


def test_equivaut_a_sklearn(matrix):
    from sklearn.preprocessing import RobustScaler

    scaler = Scaler.fit(matrix, clip=1e9)  # clip neutralise pour la comparaison
    expected = RobustScaler(quantile_range=(5.0, 95.0)).fit_transform(matrix)
    assert np.allclose(scaler.transform(matrix), expected, atol=1e-4)


def test_ecretage_borne_les_aberrations(matrix):
    scaler = Scaler.fit(matrix, clip=5.0)
    scaled = scaler.transform(matrix)
    assert np.abs(scaled).max() <= 5.0
    assert scaled.dtype == np.float32


def test_aucune_echelle_nulle(matrix):
    """Une colonne constante ne doit jamais produire une division par zero."""
    constante = matrix.copy()
    constante[:, 3] = 7.0
    scaler = Scaler.fit(constante)
    assert (scaler.scale != 0).all()
    assert np.isfinite(scaler.transform(constante)).all()


def test_refuse_un_scaler_desaligne(matrix, tmp_path):
    """Si FEATURE_NAMES change, recharger un vieux scaler doit echouer net."""
    scaler = Scaler.fit(matrix)
    path = tmp_path / "scaler.npz"
    np.savez(
        path,
        center=scaler.center,
        scale=scaler.scale,
        clip=np.array([scaler.clip]),
        feature_names=np.array(("colonne_fantome",) * len(FEATURE_NAMES), dtype=object),
    )
    with pytest.raises(ValueError, match="FEATURE_NAMES"):
        Scaler.load(path)


# --- ponderation --------------------------------------------------------------

def test_poids_par_defaut_neutres():
    assert normalise_weights(None) == {group: 1.0 for group in GROUP_NAMES}
    assert np.array_equal(
        _weight_vector(normalise_weights(None)),
        np.ones(len(FEATURE_NAMES), dtype=np.float32),
    )


def test_poids_appliques_aux_bonnes_colonnes():
    from osumapmatcher.features import FEATURE_GROUPS

    vector = _weight_vector(normalise_weights({"aim": 2.0, "parameters": 0.0}))
    for i, name in enumerate(FEATURE_NAMES):
        group = FEATURE_GROUPS[name]
        expected = {"aim": 2.0, "parameters": 0.0}.get(group, 1.0)
        assert vector[i] == expected


def test_groupe_inconnu_rejete():
    with pytest.raises(KeyError):
        normalise_weights({"jump": 2.0})


def test_tous_les_poids_a_zero_rejetes():
    with pytest.raises(ValueError):
        normalise_weights({group: 0.0 for group in GROUP_NAMES})


# --- explications -------------------------------------------------------------

def test_explain_nomme_les_ecarts_et_les_ressemblances():
    from osumapmatcher.features import FEATURE_NAMES as NAMES
    from osumapmatcher.index import explain

    position = {name: i for i, name in enumerate(NAMES)}
    query_scaled = np.zeros(len(NAMES), dtype=np.float32)
    match_scaled = np.zeros(len(NAMES), dtype=np.float32)
    query_raw = np.zeros(len(NAMES), dtype=np.float32)
    match_raw = np.zeros(len(NAMES), dtype=np.float32)

    match_scaled[position["bpm_median"]] = 2.0     # nettement plus rapide
    query_raw[position["bpm_median"]] = 144.0
    match_raw[position["bpm_median"]] = 213.0
    match_scaled[position["object_density"]] = -1.5  # nettement moins dense
    query_raw[position["object_density"]] = 4.0
    match_raw[position["object_density"]] = 2.0

    differences, similarities = explain(query_scaled, query_raw, match_scaled, match_raw)
    assert any("faster" in d and "144 BPM" in d and "213 BPM" in d for d in differences)
    assert any("less dense" in d for d in differences)
    assert len(differences) <= 3
    assert similarities  # tout le reste est a zero, donc identique


def test_explain_ne_dit_rien_sur_deux_maps_identiques():
    from osumapmatcher.features import FEATURE_NAMES as NAMES
    from osumapmatcher.index import explain

    vector = np.zeros(len(NAMES), dtype=np.float32)
    differences, similarities = explain(vector, vector, vector, vector)
    assert differences == []
    assert similarities


def test_song_key_ignore_casse_espaces_et_artiste():
    from osumapmatcher.index import song_key

    assert song_key("Camellia", "Routing") == song_key("Camellia", "  ROUTING ")
    # meme chanson, champ artiste different : doit se regrouper
    assert song_key("Aiurabu", "Kani*Do-Luck! (TV Size)") == \
           song_key("Aiurabu (CV: Nakajima Yui)", "Kani*Do-Luck!  (TV Size)")
    assert song_key("A", "Routing") != song_key("A", "Rooting")
