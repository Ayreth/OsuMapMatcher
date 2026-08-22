# -*- coding: utf-8 -*-
"""
Tests de l'API HTTP (api.py).

Ce qui compte ici n'est pas que les endpoints repondent, c'est que les trois
facons de poser la question -- checksum, dict de features, fichier .osu --
donnent exactement les memes voisins. Si elles divergent, l'API rend des
resultats plausibles et faux, et personne ne s'en apercevra.

Le reste couvre les garde-fous d'un service expose : version du jeu de
features, quota, jeton, taille d'upload, et repli annonce quand une variante
n'est pas servie.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from osumapmatcher.api import ApiConfig, create_api
from osumapmatcher.features import FEATURE_NAMES, FEATURE_VERSION


@pytest.fixture
def client(mini_index):
    """API sans quota ni jeton : le cas nominal."""
    with TestClient(create_api(mini_index, ApiConfig())) as test_client:
        yield test_client


def _first(mini_corpus) -> tuple[bytes, dict]:
    return mini_corpus[0]


def _checksums(matches) -> list[str]:
    return [match["checksum"] for match in matches]


# --- description --------------------------------------------------------------

def test_health_annonce_l_index_charge(client):
    payload = client.get("/health").json()
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["variants"] == ["nomod"]
    assert payload["feature_version"] == FEATURE_VERSION


def test_meta_decrit_le_jeu_de_features(client):
    payload = client.get("/api/v1/meta").json()
    assert payload["feature_version"] == FEATURE_VERSION
    assert payload["n_features"] == len(FEATURE_NAMES)
    assert payload["feature_names"] == list(FEATURE_NAMES)
    assert payload["n_maps"]["nomod"] > 0


def test_sans_index_l_api_ne_meurt_pas(tmp_path):
    """Un serveur sans index doit sortir du pool, pas boucler sur un crash."""
    with TestClient(create_api(tmp_path, ApiConfig())) as client:
        assert client.get("/health").json()["ready"] is False
        assert client.get("/api/v1/similar?checksum=" + "0" * 32).status_code == 503


# --- les trois chemins donnent le meme resultat --------------------------------

def test_checksum_features_et_fichier_donnent_les_memes_voisins(client, mini_corpus):
    raw, features = _first(mini_corpus)
    query = "k=5&star_range=10"

    by_checksum = client.get(
        f"/api/v1/similar?checksum={features['checksum']}&{query}"
    ).json()
    by_file = client.post(
        f"/api/v1/similar/file?{query}",
        content=raw,
        headers={"content-type": "application/octet-stream"},
    ).json()
    by_features = client.post(
        "/api/v1/similar",
        json={"features": features, "feature_version": FEATURE_VERSION,
              "k": 5, "star_range": 10.0},
    ).json()
    by_vector = client.post(
        "/api/v1/similar",
        json={
            "vector": [float(features[name]) for name in FEATURE_NAMES],
            "feature_version": FEATURE_VERSION,
            "meta": {key: features[key] for key in
                     ("stars", "checksum", "artist", "title", "version",
                      "beatmap_id", "beatmapset_id")},
            "k": 5, "star_range": 10.0,
        },
    ).json()

    assert by_checksum["count"] > 0
    reference = _checksums(by_checksum["matches"])
    assert _checksums(by_file["matches"]) == reference
    assert _checksums(by_features["matches"]) == reference
    assert _checksums(by_vector["matches"]) == reference


def test_la_source_de_la_reponse_est_annoncee(client, mini_corpus):
    raw, features = _first(mini_corpus)
    assert client.get(
        f"/api/v1/similar?checksum={features['checksum']}&star_range=10"
    ).json()["source"] == "corpus"
    # une map du corpus envoyee en entier ne coute aucune featurisation : son
    # md5 est deja son checksum
    assert client.post(
        "/api/v1/similar/file?star_range=10",
        content=raw,
        headers={"content-type": "application/octet-stream"},
    ).json()["source"] == "corpus"


def test_une_map_hors_corpus_est_featurisee(client, mini_corpus):
    raw, _ = _first(mini_corpus)
    edited = raw + b"\n"  # md5 different, donc absente du corpus
    payload = client.post(
        "/api/v1/similar/file?star_range=10",
        content=edited,
        headers={"content-type": "application/octet-stream"},
    ).json()
    assert payload["source"] == "file"
    assert payload["query"]["in_corpus"] is False
    assert payload["count"] > 0


def test_featurize_rend_un_dict_utilisable_tel_quel(client, mini_corpus):
    raw, _ = _first(mini_corpus)
    payload = client.post(
        "/api/v1/featurize",
        content=raw,
        headers={"content-type": "application/octet-stream"},
    ).json()
    assert payload["feature_version"] == FEATURE_VERSION
    assert set(FEATURE_NAMES) <= set(payload["features"])
    again = client.post(
        "/api/v1/similar",
        json={"features": payload["features"], "k": 3, "star_range": 10.0},
    )
    assert again.status_code == 200


def test_recherche_par_beatmap_id(client, mini_corpus):
    _, features = _first(mini_corpus)
    payload = client.get(
        f"/api/v1/similar?beatmap_id={int(features['beatmap_id'])}&star_range=10"
    )
    assert payload.status_code == 200
    assert payload.json()["query"]["checksum"] == features["checksum"]


def test_map_inconnue_dit_ou_envoyer_le_fichier(client):
    response = client.get("/api/v1/similar?checksum=" + "0" * 32)
    assert response.status_code == 404
    assert "/api/v1/similar/file" in response.json()["detail"]


# --- version du jeu de features ------------------------------------------------

def test_une_version_de_features_differente_est_refusee(client, mini_corpus):
    _, features = _first(mini_corpus)
    response = client.post(
        "/api/v1/similar",
        json={"features": features, "feature_version": "0000deadbeef"},
    )
    assert response.status_code == 409
    assert FEATURE_VERSION in response.json()["detail"]


def test_un_vecteur_sans_version_annoncee_est_refuse(client, mini_corpus):
    _, features = _first(mini_corpus)
    response = client.post(
        "/api/v1/similar",
        json={"vector": [float(features[name]) for name in FEATURE_NAMES]},
    )
    assert response.status_code == 400
    assert "feature_version" in response.json()["detail"]


def test_un_vecteur_de_mauvaise_taille_est_refuse(client):
    response = client.post(
        "/api/v1/similar",
        json={"vector": [0.0] * 12, "feature_version": FEATURE_VERSION},
    )
    assert response.status_code == 400
    assert str(len(FEATURE_NAMES)) in response.json()["detail"]


def test_une_feature_manquante_echoue_bruyamment(client, mini_corpus):
    _, features = _first(mini_corpus)
    amputated = {k: v for k, v in features.items() if k != "bpm_min"}
    response = client.post("/api/v1/similar", json={"features": amputated})
    assert response.status_code == 400
    assert "bpm_min" in response.json()["detail"]


def test_stars_est_obligatoire(client, mini_corpus):
    """C'est le filtre de difficulte : sans lui la recherche n'a aucun sens."""
    _, features = _first(mini_corpus)
    without = {k: v for k, v in features.items() if k != "stars"}
    response = client.post("/api/v1/similar", json={"features": without})
    assert response.status_code == 400
    assert "stars" in response.json()["detail"]


def test_des_features_calculees_avec_d_autres_mods_declenchent_un_avertissement(
    client, mini_corpus
):
    _, features = _first(mini_corpus)
    payload = client.post(
        "/api/v1/similar",
        json={"features": features, "featurized_with": "DT", "k": 3,
              "star_range": 10.0},
    ).json()
    assert any("biased" in warning for warning in payload["warnings"])


# --- variantes ------------------------------------------------------------------

def test_une_variante_non_servie_est_annoncee_pas_silencieuse(client, mini_corpus):
    _, features = _first(mini_corpus)
    payload = client.get(
        f"/api/v1/similar?checksum={features['checksum']}&mods=HDDT&star_range=10"
    ).json()
    assert payload["variant"] == "nomod"
    assert any("DT" in warning for warning in payload["warnings"])


def test_un_taux_d_horloge_libre_est_annonce(client, mini_corpus):
    _, features = _first(mini_corpus)
    payload = client.get(
        f"/api/v1/similar?checksum={features['checksum']}&rate=1.2&star_range=10"
    ).json()
    assert any("1.2" in warning for warning in payload["warnings"])


def test_une_variante_inconnue_est_une_erreur(client):
    response = client.get("/api/v1/similar?checksum=x&variant=SUPERMOD")
    assert response.status_code == 400


def test_un_groupe_de_divergence_inconnu_est_une_erreur(client, mini_corpus):
    _, features = _first(mini_corpus)
    response = client.get(
        f"/api/v1/similar?checksum={features['checksum']}&diverge=nope"
    )
    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


# --- corps de requete -----------------------------------------------------------

@pytest.mark.parametrize("body,attendu", [(b"", 400), (b"pas un .osu", 422)])
def test_corps_invalide(client, body, attendu):
    response = client.post(
        "/api/v1/similar/file",
        content=body,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == attendu


def test_le_multipart_est_refuse_avec_une_explication(client, mini_corpus):
    raw, _ = _first(mini_corpus)
    response = client.post("/api/v1/similar/file", files={"file": ("m.osu", raw)})
    assert response.status_code == 415
    assert "raw request body" in response.json()["detail"]


def test_un_fichier_trop_gros_est_refuse(mini_index, mini_corpus):
    raw, _ = _first(mini_corpus)
    config = ApiConfig(max_upload=64)
    with TestClient(create_api(mini_index, config)) as client:
        response = client.post(
            "/api/v1/similar/file",
            content=raw,
            headers={"content-type": "application/octet-stream"},
        )
    assert response.status_code == 413


# --- garde-fous d'un service expose ---------------------------------------------

def test_le_jeton_est_exige_quand_il_est_configure(mini_index, mini_corpus):
    _, features = _first(mini_corpus)
    config = ApiConfig(token="s3cret")
    url = f"/api/v1/similar?checksum={features['checksum']}&star_range=10"
    with TestClient(create_api(mini_index, config)) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer mauvais"}
                          ).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer s3cret"}
                          ).status_code == 200
        # /meta reste ouvert : un client doit pouvoir verifier la compatibilite
        # avant de savoir s'il a le droit de chercher
        assert client.get("/api/v1/meta").status_code == 200


def test_le_quota_finit_par_repondre_429(mini_index, mini_corpus):
    _, features = _first(mini_corpus)
    config = ApiConfig(rate_per_minute=60.0, burst=3.0)
    url = f"/api/v1/similar?checksum={features['checksum']}&star_range=10"
    with TestClient(create_api(mini_index, config)) as client:
        codes = [client.get(url).status_code for _ in range(6)]
    assert codes[0] == 200
    assert 429 in codes
    assert codes.count(200) <= 4


def test_le_429_dit_quand_reessayer(mini_index, mini_corpus):
    _, features = _first(mini_corpus)
    config = ApiConfig(rate_per_minute=6.0, burst=1.0)
    url = f"/api/v1/similar?checksum={features['checksum']}&star_range=10"
    with TestClient(create_api(mini_index, config)) as client:
        client.get(url)
        response = client.get(url)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_un_upload_coute_plus_cher_qu_une_recherche(mini_index, mini_corpus):
    """Featuriser coute du CPU ; le quota doit le refleter."""
    raw, features = _first(mini_corpus)
    config = ApiConfig(rate_per_minute=60.0, burst=3.0, file_cost=5.0)
    with TestClient(create_api(mini_index, config)) as client:
        response = client.post(
            "/api/v1/similar/file",
            content=raw,
            headers={"content-type": "application/octet-stream"},
        )
    assert response.status_code == 429


def test_cors_est_pose_quand_il_est_configure(mini_index):
    config = ApiConfig(cors_origins=("https://exemple.test",))
    with TestClient(create_api(mini_index, config)) as client:
        response = client.get(
            "/api/v1/meta", headers={"Origin": "https://exemple.test"}
        )
    assert response.headers["access-control-allow-origin"] == "https://exemple.test"


def test_les_stats_comptent_ce_qui_a_ete_fait(client, mini_corpus):
    raw, _ = _first(mini_corpus)
    headers = {"content-type": "application/octet-stream"}
    client.post("/api/v1/similar/file?star_range=10", content=raw + b"\n",
                headers=headers)
    client.post("/api/v1/similar/file?star_range=10", content=raw + b"\n",
                headers=headers)
    counters = client.get("/api/v1/stats").json()["counters"]
    assert counters["searches"] >= 2
    # la deuxieme fois, la featurisation vient du cache
    assert counters["featurized"] == 1
    assert counters["cache_hits"] >= 1
