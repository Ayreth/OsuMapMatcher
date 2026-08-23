# -*- coding: utf-8 -*-
"""
Le counter tosu est *genere* depuis la page du serveur local, pas ecrit a cote.

Il n'y a donc qu'une interface a maintenir. Ces tests existent pour que ca
reste vrai : une page modifiee sans regenerer le counter livrerait aux joueurs
une version qui derive silencieusement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pack_counter  # noqa: E402

PAGE = ROOT / "src" / "osumapmatcher" / "static" / "index.html"
SHIPPED = ROOT / "counter" / pack_counter.FOLDER


def test_la_page_sait_vivre_sans_le_serveur_python():
    """Les deux hotes sont dans le meme fichier, pas dans deux copies."""
    page = PAGE.read_text(encoding="utf-8")
    assert "startCounterMode" in page
    assert "startLocalMode" in page
    assert "detectLocalServer" in page
    # le counter parle au websocket de tosu et a l'API, sans intermediaire
    assert "/websocket/v2" in page
    assert "/api/v1/similar" in page


def test_le_counter_livre_est_a_jour():
    shipped = SHIPPED / "index.html"
    assert shipped.is_file(), "counter absent : lance scripts/pack_counter.py"
    assert shipped.read_text(encoding="utf-8") == PAGE.read_text(encoding="utf-8"), (
        "le counter livre ne correspond plus a la page : "
        "relance scripts/pack_counter.py"
    )


def _fiche(dossier: Path) -> dict[str, str]:
    """La fiche telle que tosu la lit : une paire `Clef: valeur` par ligne."""
    lignes = (dossier / "metadata.txt").read_text(encoding="utf-8").splitlines()
    return dict(
        (cle.strip(), valeur.strip())
        for cle, _, valeur in (ligne.partition(":") for ligne in lignes if ligne.strip())
    )


def test_le_counter_a_sa_fiche():
    fiche = _fiche(SHIPPED)
    assert fiche["Name"] == "osu!MapMatcher"
    assert fiche["Author"] == "Ayreth"
    assert fiche["Version"]


def test_la_fiche_n_est_surtout_pas_du_json():
    """
    Regression : elle l'a ete, et tosu -- qui lit ce fichier ligne a ligne --
    affichait le counter "undefined v1.0 by undefined" dans son tableau de bord.
    C'etait pire que pas de fiche du tout : sans fiche, tosu retombe au moins sur
    le nom du dossier.
    """
    brut = (SHIPPED / "metadata.txt").read_text(encoding="utf-8")
    assert not brut.lstrip().startswith("{"), "tosu ne sait pas lire du JSON"
    with pytest.raises(json.JSONDecodeError):
        json.loads(brut)
    for ligne in brut.splitlines():
        assert ":" in ligne, f"ligne sans clef : {ligne!r}"


def test_la_fiche_livree_est_celle_du_script():
    """Le dossier livre ne doit pas garder une fiche d'une version precedente."""
    assert _fiche(SHIPPED) == {
        cle: valeur for cle, valeur in pack_counter.METADATA
    }


def test_l_api_visee_peut_etre_changee_a_la_construction(tmp_path):
    built = pack_counter.build(tmp_path, "https://autre.exemple")
    page = (built / "index.html").read_text(encoding="utf-8")
    assert 'const DEFAULT_API = "https://autre.exemple"' in page
    assert "api.ayreth.site" not in page.split("const DEFAULT_API")[1][:80]


def test_sans_api_donnee_la_page_garde_la_sienne(tmp_path):
    built = pack_counter.build(tmp_path, "")
    assert (built / "index.html").read_text(encoding="utf-8") == PAGE.read_text(
        encoding="utf-8"
    )
