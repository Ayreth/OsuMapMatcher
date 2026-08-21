# -*- coding: utf-8 -*-
"""
Tests de `featurize()` : determinisme, taille du vecteur, stabilite de l'ordre.

Ce sont les seuls tests du projet a ce stade, et volontairement : une derive
silencieuse de l'ordre des colonnes entre le corpus batch et la map live est le
bug le plus probable de tout l'edifice, et le seul qu'on ne verrait jamais a
l'oeil nu dans les resultats.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest

from osureco.features import (
    FEATURE_GROUPS,
    FEATURE_NAMES,
    FEATURE_SPEC,
    GROUP_INDICES,
    GROUP_NAMES,
    Mods,
    feature_vector,
    featurize,
    featurize_detailed,
)

from osureco.paths import find_dump

#: les tests sur le vrai dump sont sautes si aucun dump n'est installe
DUMP = find_dump()


# --- fabrique de .osu synthetiques -------------------------------------------

def make_osu(
    *,
    mode: int = 0,
    # 120 objets a 500 ms font 59 s : la map reste au-dessus du seuil des 20 s
    # meme une fois acceleree par le DT
    n_objects: int = 120,
    spacing_ms: int = 500,
    header: str = "osu file format v14",
) -> bytes:
    objects = []
    for i in range(n_objects):
        x = 100 + (i % 7) * 40
        y = 80 + (i % 5) * 50
        objects.append(f"{x},{y},{i * spacing_ms},1,0,0:0:0:0:")
    body = f"""{header}

[General]
AudioFilename: audio.mp3
Mode: {mode}

[Editor]
BeatDivisor: 4

[Metadata]
Title:Synthetic
Artist:Tester
Creator:Nobody
Version:Probe
BeatmapID:12345
BeatmapSetID:678

[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:8
ApproachRate:9
SliderMultiplier:1.8
SliderTickRate:1

[Events]
2,5000,7000

[TimingPoints]
0,333.3333333333333,4,2,0,70,1,0

[HitObjects]
{chr(10).join(objects)}
"""
    return body.encode("utf-8")


@pytest.fixture(scope="module")
def synthetic() -> bytes:
    return make_osu()


@pytest.fixture(scope="module")
def dump_samples() -> list[bytes]:
    if DUMP is None or not DUMP.is_dir():
        pytest.skip("aucun dump .osu installe")
    files = sorted(DUMP.glob("*.osu"))[:400]
    rng = random.Random(1234)
    picked = rng.sample(files, min(25, len(files)))
    return [p.read_bytes() for p in picked]


# --- structure du vecteur -----------------------------------------------------

def test_noms_uniques():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


def test_chaque_feature_a_un_groupe_connu():
    assert set(FEATURE_GROUPS) == set(FEATURE_NAMES)
    assert set(FEATURE_GROUPS.values()) == set(GROUP_NAMES)


def test_les_groupes_partitionnent_le_vecteur():
    """Aucune colonne orpheline, aucune colonne comptee deux fois."""
    covered = np.concatenate([GROUP_INDICES[g] for g in GROUP_NAMES])
    assert sorted(covered.tolist()) == list(range(len(FEATURE_NAMES)))


def test_spec_et_noms_sont_alignes():
    assert tuple(name for name, _ in FEATURE_SPEC) == FEATURE_NAMES


# --- contrat de sortie --------------------------------------------------------

def test_taille_du_vecteur(synthetic):
    features = featurize(synthetic)
    assert features is not None
    vector = feature_vector(features)
    assert vector.shape == (len(FEATURE_NAMES),)
    assert vector.dtype == np.float32


def test_pas_de_nan_ni_inf(synthetic, dump_samples):
    for raw in [synthetic, *dump_samples]:
        features = featurize(raw)
        if features is None:
            continue
        vector = feature_vector(features)
        assert np.isfinite(vector).all()


def test_determinisme(synthetic, dump_samples):
    for raw in [synthetic, *dump_samples]:
        first = featurize(raw)
        second = featurize(raw)
        if first is None:
            assert second is None
            continue
        assert np.array_equal(feature_vector(first), feature_vector(second))


def test_ordre_independant_de_l_ordre_d_insertion(synthetic):
    """
    Le coeur du sujet : le vecteur doit venir de FEATURE_NAMES, pas de l'ordre
    des cles du dict. On remonte le dict a l'envers, le vecteur doit etre
    rigoureusement identique.
    """
    features = featurize(synthetic)
    assert features is not None
    shuffled = dict(reversed(list(features.items())))
    assert list(shuffled) != list(features)  # l'ordre a bien change
    assert np.array_equal(feature_vector(features), feature_vector(shuffled))


def test_cle_manquante_echoue_bruyamment(synthetic):
    features = featurize(synthetic)
    assert features is not None
    del features[FEATURE_NAMES[len(FEATURE_NAMES) // 2]]
    with pytest.raises(KeyError):
        feature_vector(features)


# --- raisons de skip ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw, attendu",
    [
        (b"", "empty_file"),
        (b"ceci n'est pas un fichier osu", "bad_header"),
        (make_osu(mode=3), "not_std"),
        (make_osu(n_objects=5), "too_few_objects"),
        (make_osu(n_objects=40, spacing_ms=100), "too_short"),
    ],
)
def test_raisons_de_skip(raw, attendu):
    result = featurize_detailed(raw)
    assert result.features is None
    assert result.skip_reason == attendu


def test_featurize_ne_leve_jamais():
    """Meme sur des octets arbitraires, on renvoie None sans exception."""
    rng = random.Random(0)
    for _ in range(50):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 500)))
        assert featurize(blob) is None
    tronque = make_osu()[: len(make_osu()) // 2]
    featurize(tronque)  # ne doit pas lever


def test_map_lente_a_un_spacing_non_nul():
    """
    Regression : sur une map tres lente, tous les ecarts depassent le seuil fixe
    de 1 s et le filtre de continuite vidait les statistiques de geometrie.
    Observe en vrai sur le beatmap 1371517 du dump.
    """
    features = featurize(make_osu(n_objects=60, spacing_ms=2000))
    assert features is not None
    assert features["sp_p50"] > 0.0
    assert features["vel_p50"] > 0.0
    assert features["dt_p50"] > 0.0


def test_timing_aberrant_ne_produit_pas_d_infini():
    """
    Regression : un beatLength denormal (present dans le dump) donnait un BPM
    infini, qui devenait `inf` a l'ecriture du parquet en float32.
    """
    raw = make_osu().replace(
        b"0,333.3333333333333,4,2,0,70,1,0",
        b"0,333.3333333333333,4,2,0,70,1,0\n1000,1e-320,4,2,0,70,1,0",
    )
    features = featurize(raw)
    assert features is not None
    vector = feature_vector(features)
    assert np.isfinite(vector).all()
    assert features["bpm_max"] < 10_000


def test_stars_est_une_metadonnee_pas_une_feature():
    """`stars` sert de filtre, elle ne doit pas peser dans la distance."""
    assert "stars" not in FEATURE_NAMES
    features = featurize(make_osu())
    assert features is not None and features["stars"] > 0


# --- mods ---------------------------------------------------------------------

def test_mods_parse():
    assert Mods.parse(None) == Mods()
    assert Mods.parse("HDDT").clock_rate == 1.5
    assert Mods.parse("NC").clock_rate == 1.5
    assert Mods.parse("HT").clock_rate == 0.75
    assert Mods.parse("HR").cs_multiplier == 1.3
    assert Mods.parse("EZ").cs_multiplier == 0.5
    assert Mods.parse(["HD", "DT"]).clock_rate == 1.5
    # un taux libre (osu!lazer) prime sur l'acronyme
    assert Mods.parse("DT", rate=1.2).clock_rate == 1.2
    # un mod inconnu ne doit jamais faire echouer la lecture
    assert Mods.parse("ZZQQ").clock_rate == 1.0


def test_variante_de_corpus_pour_des_mods():
    from osureco.features import VARIANTS, variant_for_mods

    assert variant_for_mods(None) == "nomod"
    assert variant_for_mods("") == "nomod"
    assert variant_for_mods("DT") == "DT"
    assert variant_for_mods("NC") == "DT"          # NC se comporte comme DT
    assert variant_for_mods("HDDT") == "DT"        # HD ne change pas les features
    assert variant_for_mods("HDHRDT") == "DTHR"
    assert variant_for_mods("HRDT") == "DTHR"      # l'ordre n'importe pas
    assert variant_for_mods("HR") == "HR"
    assert variant_for_mods("HT") == "HT"
    assert variant_for_mods("NFSOPF") == "nomod"   # aucun effet sur les features
    # une combinaison sans corpus dedie retombe sur la plus proche disponible
    assert variant_for_mods("HTHR") in VARIANTS
    assert variant_for_mods("EZ") == "nomod"


def test_sans_mods_le_vecteur_est_inchange(synthetic, dump_samples):
    """
    Le corpus a ete bati sans mods. Si le chemin "avec mods" modifiait le
    resultat sans mods, la map live ne serait plus comparable au corpus.
    """
    for raw in [synthetic, *dump_samples]:
        reference = featurize(raw)
        if reference is None:
            continue
        for spec in (None, "", "ZZ", Mods()):
            other = featurize(raw, spec)
            assert other is not None
            assert np.array_equal(feature_vector(reference), feature_vector(other))


def test_dt_comprime_le_temps_sans_bouger_les_objets(synthetic):
    nomod = featurize(synthetic)
    double = featurize(synthetic, "DT")
    assert nomod is not None and double is not None
    for name, ratio in (("bpm_median", 1.5), ("object_density", 1.5),
                        ("vel_p50", 1.5), ("drain_time_s", 1 / 1.5),
                        ("dt_p50", 1 / 1.5)):
        assert double[name] == pytest.approx(nomod[name] * ratio, rel=1e-3), name
    # DT ne deplace aucun objet : la geometrie pure est identique
    assert double["sp_p50"] == pytest.approx(nomod["sp_p50"], rel=1e-6)
    assert double["ang_150_180"] == pytest.approx(nomod["ang_150_180"], rel=1e-6)


def test_hr_reduit_les_cercles_donc_augmente_le_spacing_relatif(synthetic):
    nomod = featurize(synthetic)
    hard = featurize(synthetic, "HR")
    assert nomod is not None and hard is not None
    assert hard["cs"] == pytest.approx(min(nomod["cs"] * 1.3, 10.0))
    assert hard["sp_p50"] > nomod["sp_p50"]
    assert hard["bpm_median"] == pytest.approx(nomod["bpm_median"])


def test_cs_plafonne_a_dix():
    features = featurize(make_osu().replace(b"CircleSize:4", b"CircleSize:9"), "HR")
    assert features is not None
    assert features["cs"] == pytest.approx(10.0)


def test_metadonnees_presentes(synthetic):
    features = featurize(synthetic)
    assert features is not None
    assert features["beatmap_id"] == 12345
    assert features["beatmapset_id"] == 678
    assert features["artist"] == "Tester"
    assert features["title"] == "Synthetic"
    assert len(features["checksum"]) == 32
