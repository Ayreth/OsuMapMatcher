# -*- coding: utf-8 -*-
"""
Tests de la reprise du corpus quand le dossier source a bouge.

Le cas qui justifie ce fichier : elargir le pool non classe. La reprise est
indexee par POSITION -- tranche 0 = les 50 000 premiers fichiers de
`sorted(glob("*.osu"))`. Passer de 10 000 a 40 000 fichiers laisse toujours une
seule tranche, la tranche 0, deja presente et deja marquee terminee.

Sans le controle du nombre de fichiers, elle etait donc reutilisee telle quelle
et les 30 000 fichiers ajoutes n'etaient jamais featurises. Rien ne le
signalait : le compte annonce au demarrage etait bien le nouveau, l'index se
construisait, il etait valide -- il lui manquait juste tout ce qu'on venait
d'ajouter. Une heure de telechargement pour rien, et un deploiement qui a l'air
reussi.
"""

from __future__ import annotations

import json

import pytest

from osumapmatcher.corpus import build_corpus, load_corpus, resume_is_valid


def _manifeste(dossier, *, source, n_seen, n_kept=None, mods="", skips=None):
    n_kept = n_seen if n_kept is None else n_kept
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "manifest.json").write_text(
        json.dumps({
            "source": str(source),
            "mods": mods,
            "n_seen": n_seen,
            "n_kept": n_kept,
            "skips": skips or {},
        }),
        encoding="utf-8",
    )


def _pool(dossier, n):
    dossier.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (dossier / f"{1000 + i}.osu").write_bytes(b"osu file format v14\n")
    return dossier


# --- le predicat --------------------------------------------------------------

def test_un_dossier_inchange_reste_reprenable(tmp_path):
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    _manifeste(out, source=source, n_seen=10)

    assert resume_is_valid(out, source, None) is True


def test_un_dossier_qui_a_grossi_invalide_la_reprise(tmp_path):
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    _manifeste(out, source=source, n_seen=10)
    _pool(source, 30)

    assert resume_is_valid(out, source, None) is False, (
        "la tranche 0 couvre les dix premiers fichiers du tri ; en reutiliser "
        "une qui ne decrit qu'un tiers du dossier passerait inapercu"
    )


def test_un_dossier_qui_a_maigri_invalide_aussi(tmp_path):
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    _manifeste(out, source=source, n_seen=10)
    (source / "1000.osu").unlink()

    assert resume_is_valid(out, source, None) is False


def test_les_skips_comptent_dans_le_nombre_de_fichiers_vus(tmp_path):
    # un fichier skippe a bien ete VU : le manifeste compte 10, pas 7
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    _manifeste(out, source=source, n_seen=10, n_kept=7,
               skips={"too_short": 3})

    assert resume_is_valid(out, source, None) is True


def test_un_manifeste_incoherent_reste_refuse(tmp_path):
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    _manifeste(out, source=source, n_seen=10, n_kept=4)  # 4 + 0 != 10

    assert resume_is_valid(out, source, None) is False


def test_sans_manifeste_il_n_y_a_rien_a_contredire(tmp_path):
    # une construction interrompue n'ecrit pas de manifeste : ses tranches
    # doivent rester reprenables, c'est tout l'interet de la reprise
    source = _pool(tmp_path / "pool", 10)
    out = tmp_path / "corpus"
    out.mkdir()

    assert resume_is_valid(out, source, None) is True


# --- de bout en bout ----------------------------------------------------------

@pytest.mark.parametrize("mods", [None])
def test_les_fichiers_ajoutes_finissent_dans_le_corpus(tmp_path, mini_corpus, mods):
    source = tmp_path / "pool"
    source.mkdir()
    out = tmp_path / "corpus"

    for i, (raw, _) in enumerate(mini_corpus[:6]):
        (source / f"{2000 + i}.osu").write_bytes(raw)
    premier = build_corpus(source, out, workers=1, resume=True,
                           progress=False, mods=mods)

    for i, (raw, _) in enumerate(mini_corpus[6:12], start=6):
        (source / f"{2000 + i}.osu").write_bytes(raw)
    second = build_corpus(source, out, workers=1, resume=True,
                          progress=False, mods=mods)

    assert second.n_seen == 12
    assert second.n_kept > premier.n_kept
    assert len(load_corpus(out)) == second.n_kept, (
        "le corpus sur le disque doit contenir ce que le rapport annonce"
    )
