# -*- coding: utf-8 -*-
"""
La recherche directionnelle vue du HTTP.

Ce qui compte ici n'est pas que le moteur dose bien -- test_search_steer.py s'en
charge -- mais que le reglage arrive intact par les TROIS endpoints, sous ses
deux formes (dict JSON et chaine compacte), et que ce qui est mal ecrit ressorte
en 400 plutot qu'en 500.

Le decoupage suit celui de test_api_multi.py : un fichier par sujet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from osumapmatcher.api import STATIC_DIR, ApiConfig, create_api, parse_steer
from osumapmatcher.index import MAX_STEER, STEER_AXES

AXE = "object_density"
#: les six axes montres dans /explore. Les neuf autres restent accessibles par
#: l'API : quinze sliders sont une interface que personne n'ouvre.
AXES_DE_LA_PAGE = ("object_density", "dt_frac_lt100", "sp_p50", "vel_p50",
                   "aim_speed_ratio", "drain_time_s")
LARGE = {"k": 5, "star_range": 10.0, "length_min": 0.05, "length_max": 20.0}


@pytest.fixture
def client(mini_index):
    with TestClient(create_api(mini_index, ApiConfig())) as test_client:
        yield test_client


@pytest.fixture
def corpus(mini_corpus) -> list[dict]:
    return [features for _raw, features in mini_corpus]


@pytest.fixture
def cible(corpus) -> dict:
    """La map la moins dense : c'est elle qui laisse de la place au-dessus."""
    return min(corpus, key=lambda f: float(f[AXE]))


def _densites(payload, corpus) -> list[float]:
    par_checksum = {f["checksum"]: float(f[AXE]) for f in corpus}
    return [par_checksum[m["checksum"]] for m in payload["matches"]]


# --- description ---------------------------------------------------------------

def test_meta_annonce_la_direction_et_son_vocabulaire(client):
    """
    Une page publiee avant le redeploiement du VPS doit pouvoir masquer les
    sliders au lieu de prendre un 400. Et le vocabulaire est servi plutot que
    recopie dans la page : les memes mots que sous chaque resultat.
    """
    payload = client.get("/api/v1/meta").json()
    assert "steer" in payload["supports"]
    assert payload["limits"]["steer_max"] == MAX_STEER

    axes = {entry["axis"]: (entry["more"], entry["less"])
            for entry in payload["steer_axes"]}
    assert axes == STEER_AXES
    assert axes[AXE] == ("denser", "less dense")


# --- forme compacte -----------------------------------------------------------

def test_la_forme_compacte_se_lit_comme_un_dict():
    assert parse_steer("object_density:+0.3,sp_p50:-0.5") == {
        "object_density": 0.3, "sp_p50": -0.5,
    }
    assert parse_steer(" object_density : 0.3 ") == {"object_density": 0.3}
    assert parse_steer("") == {}
    assert parse_steer("object_density:0.3,") == {"object_density": 0.3}


@pytest.mark.parametrize("texte", ["object_density", "object_density:", ":0.3",
                                   "object_density:beaucoup"])
def test_une_forme_compacte_illisible_est_refusee(texte):
    with pytest.raises(ValueError):
        parse_steer(texte)


# --- les trois endpoints ------------------------------------------------------

def test_get_similar_accepte_la_forme_compacte(client, cible, corpus):
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:+0.3", **LARGE,
    })
    assert reponse.status_code == 200
    payload = reponse.json()
    assert payload["matches"], "le mini corpus doit avoir des candidates"
    for densite in _densites(payload, corpus):
        assert densite > float(cible[AXE])


def test_post_similar_accepte_le_dict_et_rend_la_meme_chose(client, cible, corpus):
    """
    Les deux formes sont deux ecritures du meme reglage. Si elles divergeaient,
    la page et les scripts n'auraient plus le meme moteur.
    """
    compact = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:+0.3", **LARGE,
    }).json()
    dictionnaire = client.post("/api/v1/similar", json={
        "checksum": cible["checksum"], "steer": {AXE: 0.3}, **LARGE,
    }).json()
    assert [m["checksum"] for m in dictionnaire["matches"]] == \
           [m["checksum"] for m in compact["matches"]]


def test_similar_multi_accepte_aussi_la_direction(client, corpus):
    graines = sorted(corpus, key=lambda f: float(f[AXE]))[:2]
    reponse = client.post("/api/v1/similar/multi", json={
        "checksums": [f["checksum"] for f in graines],
        "steer": {AXE: 0.2}, **LARGE,
    })
    assert reponse.status_code == 200
    payload = reponse.json()
    assert payload["matches"]
    assert payload["steer"] == {AXE: 0.2}


def test_similar_file_accepte_la_direction_en_query(client, mini_corpus, corpus):
    """L'upload n'a que la query string pour porter ses reglages."""
    brut, features = min(mini_corpus, key=lambda row: float(row[1][AXE]))
    reponse = client.post(
        "/api/v1/similar/file",
        params={"steer": f"{AXE}:+0.3", **LARGE},
        content=brut,
        headers={"content-type": "application/octet-stream"},
    )
    assert reponse.status_code == 200
    payload = reponse.json()
    assert payload["matches"]
    for densite in _densites(payload, corpus):
        assert densite > float(features[AXE])


# --- ce que la reponse dit ----------------------------------------------------

def test_la_reponse_renvoie_la_direction_telle_qu_elle_a_ete_comprise(client, cible):
    """
    Meme raison que pour le repoussoir : un client qui a envoye la forme
    compacte doit pouvoir verifier ce que le serveur en a fait.
    """
    payload = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:+0.3", **LARGE,
    }).json()
    assert payload["steer"] == {AXE: 0.3}


def test_sans_direction_la_reponse_le_dit_par_un_null(client, cible):
    payload = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], **LARGE,
    }).json()
    assert payload["steer"] is None


def test_une_direction_au_repos_ne_change_rien(client, cible):
    """Une page a sliders envoie des zeros a chaque recherche."""
    sans = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], **LARGE,
    }).json()
    zero = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:0", **LARGE,
    }).json()
    assert [m["checksum"] for m in zero["matches"]] == \
           [m["checksum"] for m in sans["matches"]]
    assert zero["steer"] is None


def test_la_direction_change_reellement_le_resultat(client, cible):
    sans = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], **LARGE,
    }).json()
    avec = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:+0.3", **LARGE,
    }).json()
    assert [m["checksum"] for m in avec["matches"]] != \
           [m["checksum"] for m in sans["matches"]]


# --- ce que l'API refuse ------------------------------------------------------

def test_un_axe_inconnu_rend_400_et_pas_500(client, cible):
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": "nawak:+0.3", **LARGE,
    })
    assert reponse.status_code == 400
    assert "nawak" in reponse.json()["detail"]


def test_une_feature_hors_vocabulaire_rend_400(client, cible):
    """`bpm_min` est une vraie colonne, mais rien ne sait la dire."""
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": "bpm_min:+0.3", **LARGE,
    })
    assert reponse.status_code == 400


def test_une_forme_illisible_rend_400_et_pas_500(client, cible):
    """
    Regression : le modele est construit dans le corps de la dependance, donc
    FastAPI ne voit pas passer l'erreur de validation et rendrait un 500.
    """
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": "object_density=0.3", **LARGE,
    })
    assert reponse.status_code == 400


@pytest.mark.parametrize("amplitude", ["+1.5", "-1.5"])
def test_une_amplitude_hors_bornes_rend_400(client, cible, amplitude):
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": f"{AXE}:{amplitude}", **LARGE,
    })
    assert reponse.status_code == 400


def test_un_axe_sans_aucune_candidate_rend_une_liste_vide_pas_une_erreur(client,
                                                                         cible):
    """`cs` est constant dans tout le mini corpus : personne au-dessus."""
    reponse = client.get("/api/v1/similar", params={
        "checksum": cible["checksum"], "steer": "cs:+0.3", **LARGE,
    })
    assert reponse.status_code == 200
    assert reponse.json()["matches"] == []


# --- les deux pages -----------------------------------------------------------

def test_la_page_montre_six_sliders_et_lit_les_mots_chez_le_serveur(client):
    """
    Les mots viennent de /meta, pas d'une liste recopiee dans la page. Recopies,
    ils finiraient par ne plus dire la meme chose que les phrases affichees sous
    chaque resultat -- et l'interface promettrait une chose en montrant l'autre.
    """
    body = client.get("/explore").text
    assert 'id="panelS"' in body and 'id="steer"' in body
    assert "steer_axes" in body, "la page doit lire le vocabulaire dans /meta"
    for axis in AXES_DE_LA_PAGE:
        assert axis in body, f"{axis} devrait avoir son slider"


def test_l_overlay_a_les_memes_sliders_et_le_meme_vocabulaire():
    """
    L'overlay a d'abord ete laisse de cote -- pas de champ de saisie, un coin
    d'ecran secondaire -- puis reclame quand meme. Il porte donc les six memes
    axes, batis comme sur /explore : depuis /meta, pas depuis une liste ecrite
    dans la page. Deux interfaces du meme outil ne doivent pas avoir l'air
    d'outils differents, ni dire deux vocabulaires.
    """
    overlay = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="panelS"' in overlay and 'id="steer"' in overlay
    assert "steer_axes" in overlay, "le vocabulaire doit venir de /meta"
    for axis in AXES_DE_LA_PAGE:
        assert axis in overlay, f"{axis} devrait avoir son slider"
    # il reste servable par tosu seul : un seul fichier, aucune ressource externe
    assert "cdn." not in overlay and "fonts.googleapis" not in overlay


def test_les_deux_pages_montrent_exactement_les_memes_axes():
    """
    Regression : un axe ajoute d'un cote et pas de l'autre ferait deux outils.
    Les listes sont ecrites deux fois -- les pages sont deux fichiers autonomes,
    c'est le prix connu -- donc quelque chose doit les comparer.
    """
    page = (STATIC_DIR / "explore.html").read_text(encoding="utf-8")
    overlay = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def axes(texte: str) -> list[str]:
        bloc = texte.split("const STEER_ROWS = [", 1)[1].split("];", 1)[0]
        return [ligne.split('"')[1] for ligne in bloc.splitlines() if '["' in ligne]

    assert axes(page) == axes(overlay) == list(AXES_DE_LA_PAGE)
