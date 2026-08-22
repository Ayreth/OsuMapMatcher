# -*- coding: utf-8 -*-
"""
Tests HTTP des trois nouveautes : /similar/multi, le filtre par groupe et le
contre-exemple.

Ce qui compte ici n'est pas que les endpoints repondent, c'est que la reponse
garde LA MEME FORME partout. Un client qui lit `query.seeds` ne doit pas avoir a
savoir par quel endpoint il est passe, sinon la page finit avec deux chemins de
rendu et un seul des deux est teste.

Le reste verifie ce que l'API refuse et ce qu'elle avoue : une graine absente
n'est pas une erreur mais doit se lire dans `warnings`, un repoussoir absent en
est une, et un centroide etale sur des etoiles tres eloignees doit le dire.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from osumapmatcher.api import MAX_SEEDS, ApiConfig, create_api
from osumapmatcher.index import DEFAULT_AVOID_STRENGTH, MAX_AVOID_STRENGTH


@pytest.fixture
def client(mini_index):
    with TestClient(create_api(mini_index, ApiConfig())) as test_client:
        yield test_client


@pytest.fixture
def sums(mini_corpus) -> list[str]:
    return [features["checksum"] for _raw, features in mini_corpus]


@pytest.fixture
def ids(mini_corpus) -> list[int]:
    return [int(features["beatmap_id"]) for _raw, features in mini_corpus]


LARGE = {"k": 5, "star_range": 10.0, "length_min": 0.05, "length_max": 20.0}


def _checksums(payload) -> list[str]:
    return [match["checksum"] for match in payload["matches"]]


# --- description ---------------------------------------------------------------

def test_meta_annonce_ce_que_le_serveur_sait_faire(client):
    """
    Une page publiee avant le redeploiement du serveur tombe sur une API qui
    ignore /similar/multi. Elle doit pouvoir le lire ici plutot que de le
    decouvrir sur un 404.
    """
    payload = client.get("/api/v1/meta").json()
    assert {"multi", "group_filter", "avoid"} <= set(payload["supports"])
    assert payload["limits"]["max_seeds"] == MAX_SEEDS
    assert payload["limits"]["avoid_strength_max"] == MAX_AVOID_STRENGTH


# --- recherche multi-graines ---------------------------------------------------

def test_multi_rend_la_meme_forme_que_les_autres_endpoints(client, sums):
    payload = client.post(
        "/api/v1/similar/multi", json={"checksums": sums[:3], **LARGE}
    ).json()
    solo = client.get(f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10").json()
    assert set(payload) == set(solo)
    assert set(payload["query"]) == set(solo["query"])
    assert payload["count"] == len(payload["matches"]) == 5
    assert payload["variant"] == "nomod"
    assert payload["warnings"] == []


def test_multi_decrit_ses_graines_et_s_annonce_comme_centroide(client, sums):
    payload = client.post(
        "/api/v1/similar/multi", json={"checksums": sums[:3], **LARGE}
    ).json()
    query = payload["query"]
    assert query["is_centroid"] is True
    assert query["n_seeds"] == 3
    assert [seed["checksum"] for seed in query["seeds"]] == sums[:3]
    assert query["label"] == "centroid of 3 maps"
    # un centroide n'est aucune map : ni checksum, ni presence dans le corpus
    assert query["checksum"] == ""
    assert query["in_corpus"] is False
    assert query["star_span"][0] <= query["stars"] <= query["star_span"][1]


def test_une_recherche_a_une_map_decrit_quand_meme_sa_graine(client, sums):
    """La meme forme partout : une graine, pas zero, et is_centroid a faux."""
    query = client.get(
        f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10"
    ).json()["query"]
    assert query["is_centroid"] is False
    assert query["n_seeds"] == 0
    assert query["seeds"] == []


def test_multi_accepte_checksums_et_beatmap_ids_melanges(client, sums, ids):
    """Un lien osu! donne un id, une map lue en jeu donne un checksum."""
    payload = client.post(
        "/api/v1/similar/multi",
        json={"checksums": sums[:2], "beatmap_ids": ids[5:7], **LARGE},
    ).json()
    assert payload["query"]["n_seeds"] == 4
    assert payload["count"] == 5


def test_multi_a_une_seule_graine_donne_la_meme_chose_que_similar(client, sums):
    multi = client.post(
        "/api/v1/similar/multi", json={"checksums": [sums[0]], **LARGE}
    ).json()
    solo = client.get(f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10").json()
    assert _checksums(multi) == _checksums(solo)
    assert multi["query"]["is_centroid"] is False


def test_multi_ne_recommande_aucune_de_ses_graines(client, sums):
    payload = client.post(
        "/api/v1/similar/multi", json={"checksums": sums[:4], "k": 20,
                                       "star_range": 10.0, "length_min": 0.05,
                                       "length_max": 20.0, "max_per_song": 0}
    ).json()
    assert not set(_checksums(payload)) & set(sums[:4])


def test_une_graine_absente_du_corpus_est_ignoree_et_annoncee(client, sums):
    """
    Perdre une graine sur trois laisse une reponse qui vaut encore quelque
    chose -- a condition de le dire. La taire serait le contraire.
    """
    payload = client.post(
        "/api/v1/similar/multi",
        json={"checksums": [sums[0], "f" * 32, sums[1]], **LARGE},
    ).json()
    assert payload["query"]["n_seeds"] == 2
    assert any("not in this corpus" in w for w in payload["warnings"])
    assert any("f" * 32 in w for w in payload["warnings"])


def test_aucune_graine_connue_est_un_404(client):
    response = client.post(
        "/api/v1/similar/multi", json={"checksums": ["f" * 32, "e" * 32], **LARGE}
    )
    assert response.status_code == 404


def test_une_graine_repetee_ne_pese_pas_double(client, sums):
    """
    Le centroide ne pondere pas ses graines -- c'est une decision. Un doublon
    la contournerait par accident, donc il est retire et signale.
    """
    payload = client.post(
        "/api/v1/similar/multi",
        json={"checksums": [sums[0], sums[1], sums[0]], **LARGE},
    ).json()
    assert payload["query"]["n_seeds"] == 2
    assert any("twice" in w for w in payload["warnings"])

    sans_doublon = client.post(
        "/api/v1/similar/multi", json={"checksums": sums[:2], **LARGE}
    ).json()
    assert _checksums(payload) == _checksums(sans_doublon)


def test_multi_sans_graine_est_refuse(client):
    assert client.post("/api/v1/similar/multi", json=LARGE).status_code == 400


def test_multi_borne_le_nombre_de_graines(client, sums):
    trop = [f"{i:032x}" for i in range(MAX_SEEDS + 1)]
    response = client.post("/api/v1/similar/multi", json={"checksums": trop, **LARGE})
    assert response.status_code == 400
    assert "Too many seeds" in response.json()["detail"]


def test_des_graines_tres_eloignees_le_disent(client, mini_corpus):
    """
    Mesure sur l'index reel : des graines a 3,0 et 8,0 etoiles rendent des
    resultats a 6 etoiles. Le centroide fait son travail, mais personne n'a
    demande du 6 etoiles -- il faut donc le dire.
    """
    par_etoiles = sorted(mini_corpus, key=lambda row: row[1]["stars"])
    extremes = [par_etoiles[0][1]["checksum"], par_etoiles[-1][1]["checksum"]]
    payload = client.post(
        "/api/v1/similar/multi", json={"checksums": extremes, **LARGE}
    ).json()
    ecart = payload["query"]["star_span"][1] - payload["query"]["star_span"][0]
    attendu = any("compromise" in w for w in payload["warnings"])
    # le petit corpus n'est peut-etre pas assez etale pour declencher l'alerte :
    # ce qui est teste, c'est la coherence entre l'ecart et l'avertissement
    assert attendu == (ecart > 2.0)


# --- filtre par groupe dominant ------------------------------------------------

def test_le_filtre_par_groupe_passe_par_l_url(client, sums):
    payload = client.get(
        f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10"
        "&length_min=0.05&length_max=20&group_filter=spacing"
    ).json()
    assert payload["group_filter"] == "spacing"
    for match in payload["matches"]:
        assert max(match["group_share"], key=match["group_share"].get) == "spacing"


def test_le_filtre_par_groupe_marche_aussi_en_multi(client, sums):
    payload = client.post(
        "/api/v1/similar/multi",
        json={"checksums": sums[:3], "group_filter": "spacing", **LARGE},
    ).json()
    assert payload["group_filter"] == "spacing"
    for match in payload["matches"]:
        assert max(match["group_share"], key=match["group_share"].get) == "spacing"


def test_un_groupe_de_filtrage_inconnu_rend_400(client, sums):
    response = client.get(
        f"/api/v1/similar?checksum={sums[0]}&group_filter=nawak&star_range=10"
    )
    assert response.status_code == 400
    assert "group filter" in response.json()["detail"].lower()


# --- contre-exemple ------------------------------------------------------------

def test_le_contre_exemple_passe_par_l_url_et_est_renvoye(client, sums):
    payload = client.get(
        f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10"
        f"&length_min=0.05&length_max=20&avoid_checksum={sums[1]}"
        "&exclude_same_set=false"
    ).json()
    assert payload["avoid"]["checksum"] == sums[1]
    assert payload["avoid"]["strength"] == DEFAULT_AVOID_STRENGTH
    assert payload["avoid"]["label"]
    assert sums[1] not in _checksums(payload)


def test_sans_contre_exemple_le_champ_reste_nul(client, sums):
    payload = client.get(f"/api/v1/similar?checksum={sums[0]}&star_range=10").json()
    assert payload["avoid"] is None


def test_le_contre_exemple_se_nomme_aussi_par_beatmap_id(client, sums, ids):
    payload = client.get(
        f"/api/v1/similar?checksum={sums[0]}&k=5&star_range=10"
        f"&length_min=0.05&length_max=20&avoid_beatmap_id={ids[1]}"
        "&exclude_same_set=false"
    ).json()
    # renvoye tel qu'il a ete COMPRIS : le client doit pouvoir verifier sur
    # quelle map le serveur est reellement tombe
    assert payload["avoid"]["beatmap_id"] == ids[1]
    assert payload["avoid"]["checksum"] == sums[1]


def test_un_contre_exemple_introuvable_est_une_erreur(client, sums):
    """
    L'ignorer rendrait une liste qui a l'air juste et qui laisse tomber la seule
    contrainte que l'utilisateur a pris la peine d'exprimer.
    """
    response = client.get(
        f"/api/v1/similar?checksum={sums[0]}&avoid_checksum={'f' * 32}&star_range=10"
    )
    assert response.status_code == 404
    assert "avoid" in response.json()["detail"].lower()


def test_une_force_de_repulsion_hors_bornes_est_refusee(client, sums):
    response = client.get(
        f"/api/v1/similar?checksum={sums[0]}&avoid_checksum={sums[1]}"
        "&avoid_strength=0.95&star_range=10"
    )
    assert response.status_code == 422  # borne portee par le modele


def test_le_contre_exemple_marche_aussi_en_multi(client, sums):
    payload = client.post(
        "/api/v1/similar/multi",
        json={"checksums": sums[:2], "avoid_checksum": sums[5],
              "exclude_same_set": False, **LARGE},
    ).json()
    assert payload["avoid"]["checksum"] == sums[5]
    assert sums[5] not in _checksums(payload)


def test_le_contre_exemple_marche_aussi_en_post(client, sums):
    payload = client.post(
        "/api/v1/similar",
        json={"checksum": sums[0], "avoid_checksum": sums[1],
              "exclude_same_set": False, **LARGE},
    ).json()
    assert payload["avoid"]["checksum"] == sums[1]
    assert sums[1] not in _checksums(payload)


def test_le_contre_exemple_ne_change_pas_la_similarite_affichee(client, sums):
    """
    Le score penalise classe, il ne s'affiche pas. La distance rendue reste
    celle qui separe le resultat de la CIBLE.
    """
    commun = ("k=5&star_range=10&length_min=0.05&length_max=20"
              "&exclude_same_set=false&max_per_song=0")
    sans = client.get(f"/api/v1/similar?checksum={sums[0]}&{commun}").json()
    avec = client.get(
        f"/api/v1/similar?checksum={sums[0]}&{commun}&avoid_checksum={sums[12]}"
    ).json()
    reference = {m["checksum"]: m["distance"] for m in sans["matches"]}
    communs = [m for m in avec["matches"] if m["checksum"] in reference]
    assert communs, "les deux listes doivent partager au moins un resultat"
    for match in communs:
        assert match["distance"] == pytest.approx(reference[match["checksum"]])


# --- symetrie serveur local / API hebergee -------------------------------------

def test_le_serveur_local_et_l_api_hebergee_exposent_le_meme_routeur(
    mini_index, monkeypatch
):
    """
    Les deux hotes montent `build_router` : un client bascule de l'un a l'autre
    sans changer une ligne. Cette symetrie ne tient que tant que personne
    n'ajoute un endpoint d'un seul cote -- ce test est la pour que la premiere
    tentative de le faire echoue ici plutot que chez un utilisateur.
    """
    from osumapmatcher import server as server_module

    class _SansTosu:
        connected = False
        last_error = ""
        base_url = ""

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self) -> None:
            return None

    monkeypatch.setattr(server_module, "TosuWatcher", _SansTosu)
    local = server_module.create_app(mini_index, api_url="", mode="local")
    hosted = create_api(mini_index, ApiConfig())

    def routes(app) -> set[tuple[str, str]]:
        """Ce que le client voit, pas ce que starlette range en interne."""
        return {
            (path, method)
            for path, methods in app.openapi()["paths"].items()
            if path.startswith("/api/v1")
            for method in methods
        }

    assert routes(local) == routes(hosted)
    assert ("/api/v1/similar/multi", "post") in routes(local)
