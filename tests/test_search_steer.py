# -*- coding: utf-8 -*-
"""
La recherche directionnelle : "comme celle-ci, mais plus dense".

Un fichier par sujet, comme test_search_multi.py. Ce qui est verifie ici tient
en une phrase : la marge demandee est reellement franchie, elle ne deforme ni la
distance affichee ni le vocabulaire, et elle tient avec tout le reste.

Le mini corpus n'a de variance que sur quelques colonnes -- `object_density`,
`vel_p50`, `drain_time_s`, `aim_hot`. Les axes plats (`cs`, `ar`, `sp_p50`...)
servent au cas "aucune candidate ne franchit la marge", qui est une reponse.
"""

from __future__ import annotations

import numpy as np
import pytest

from osumapmatcher import index as index_module
from osumapmatcher.index import (
    EXPLAIN_SPEC,
    MAX_STEER,
    STEER_AXES,
    QueryTarget,
    Searcher,
    combine_targets,
    normalise_steer,
)
from osumapmatcher.features import FEATURE_NAMES

#: le petit corpus n'a ni bande d'etoiles ni duree realistes
LARGE = dict(star_range=10.0, length_ratio=None)

#: la colonne qui bouge le plus dans le mini corpus, donc la seule ou une marge
#: de 0,9 unite robuste laisse encore quelqu'un
AXE = "object_density"
POSITION = FEATURE_NAMES.index(AXE)


@pytest.fixture(scope="module")
def searcher(mini_index) -> Searcher:
    found = Searcher(mini_index / "nomod")
    yield found
    found.close()


def _checksums(searcher: Searcher) -> list[str]:
    return [row["checksum"] for row in
            searcher.db.execute("SELECT checksum FROM maps ORDER BY pos")]


def _target(searcher: Searcher, position: int) -> QueryTarget:
    return searcher.target_from_checksum(_checksums(searcher)[position])


def _le_moins_dense(searcher: Searcher) -> QueryTarget:
    """La cible qui laisse le plus de place au-dessus d'elle sur l'axe."""
    colonne = np.asarray(searcher.vectors, dtype=np.float32)[:, POSITION]
    return _target(searcher, int(np.argmin(colonne)))


def _ecart(searcher: Searcher, cible: QueryTarget, checksum: str) -> float:
    """Ecart scale sur l'axe, entre un resultat et la cible."""
    position = searcher._position_of(checksum)
    return float(searcher.vectors[position][POSITION]) - float(cible.vector[POSITION])


# --- vocabulaire --------------------------------------------------------------

def test_le_vocabulaire_vient_d_explain_spec_et_de_nulle_part_ailleurs():
    """
    Decision structurante : un reglage "denser" et l'explication "denser
    (2.4 -> 3.9 obj/s)" doivent designer la meme colonne. Deux listes paralleles
    finiraient par diverger, et l'interface promettrait une chose en montrant
    l'autre.
    """
    attendu = {name: (more, less) for name, more, less, _same, _fmt in EXPLAIN_SPEC}
    assert STEER_AXES == attendu
    assert set(STEER_AXES) <= set(FEATURE_NAMES)


def test_un_axe_inconnu_est_refuse(searcher):
    with pytest.raises(KeyError):
        searcher.search(_target(searcher, 0), k=3, steer={"nawak": 0.3}, **LARGE)


def test_une_feature_hors_du_vocabulaire_est_refusee(searcher):
    """
    `bpm_min` est une vraie colonne du vecteur, mais elle n'a pas de nom lisible
    dans EXPLAIN_SPEC. La laisser passer creerait un axe que rien ne sait dire.
    """
    assert "bpm_min" in FEATURE_NAMES and "bpm_min" not in STEER_AXES
    with pytest.raises(KeyError):
        searcher.search(_target(searcher, 0), k=3, steer={"bpm_min": 0.3}, **LARGE)


# --- amplitude ----------------------------------------------------------------

def test_une_amplitude_nulle_est_un_reglage_au_repos_pas_une_erreur(searcher):
    """
    Une interface a sliders envoie des zeros a chaque recherche. Ils doivent
    disparaitre ici plutot que d'etre reconnus partout ailleurs.
    """
    assert normalise_steer({AXE: 0.0, "cs": -0.0}) == {}
    cible = _target(searcher, 0)
    sans = [m.checksum for m in searcher.search(cible, k=5, **LARGE)]
    avec = [m.checksum for m in searcher.search(
        cible, k=5, steer={AXE: 0.0}, **LARGE)]
    assert avec == sans


def test_l_amplitude_maximale_est_acceptee(searcher):
    """Le bord du domaine est dedans, pas dehors : un slider a fond doit passer."""
    cible = _le_moins_dense(searcher)
    assert normalise_steer({AXE: MAX_STEER}) == {AXE: MAX_STEER}
    # 1,0 unite robuste depasse la variance du mini corpus : la reponse est une
    # liste vide, et c'est une reponse -- ce qui compte est qu'aucune erreur ne
    # soit levee au bord du domaine.
    searcher.search(cible, k=3, steer={AXE: MAX_STEER}, **LARGE)


@pytest.mark.parametrize("amplitude", [1.01, -1.01, 5.0, float("nan"), float("inf")])
def test_une_amplitude_hors_bornes_est_refusee(searcher, amplitude):
    """
    Au-dela de MAX_STEER, mesure sur l'index reel : la moitie des axes ne garde
    aucun candidat, et les autres ne rendent que des maps dont plusieurs
    coordonnees sont ecretees a +/-5, c'est-a-dire les aberrations du corpus.
    """
    with pytest.raises(ValueError):
        searcher.search(_target(searcher, 0), k=3,
                        steer={AXE: amplitude}, **LARGE)


# --- ce que la marge garantit -------------------------------------------------

@pytest.mark.parametrize("delta", [0.2, 0.4, 0.6])
def test_la_marge_demandee_est_reellement_franchie(searcher, delta):
    cible = _le_moins_dense(searcher)
    matches = searcher.search(cible, k=10, steer={AXE: delta}, **LARGE)
    assert matches, f"le mini corpus doit avoir des candidates a +{delta}"
    for match in matches:
        assert _ecart(searcher, cible, match.checksum) >= delta - 1e-6


def test_la_direction_negative_va_bien_dans_l_autre_sens(searcher):
    colonne = np.asarray(searcher.vectors, dtype=np.float32)[:, POSITION]
    cible = _target(searcher, int(np.argmax(colonne)))
    matches = searcher.search(cible, k=10, steer={AXE: -0.4}, **LARGE)
    assert matches
    for match in matches:
        assert _ecart(searcher, cible, match.checksum) <= -0.4 + 1e-6


def test_l_ecart_obtenu_se_pose_sur_la_marge_au_lieu_de_la_survoler(searcher):
    """
    C'est ce qui fait qu'un filtre a marge DOSE la ou un filtre de signe ne dose
    rien : classer par distance a la cible prefere le plus petit depassement
    possible. Mesure sur l'index reel, axe densite : +0,59 obj/s a delta=0,1,
    +1,54 a 0,3, +2,54 a 0,5.
    """
    cible = _le_moins_dense(searcher)
    obtenus = []
    for delta in (0.2, 0.4, 0.6):
        matches = searcher.search(cible, k=5, steer={AXE: delta}, **LARGE)
        assert matches
        obtenus.append(min(_ecart(searcher, cible, m.checksum) for m in matches))
    assert obtenus[0] < obtenus[1] < obtenus[2], (
        f"l'ecart obtenu doit suivre la marge demandee, vu {obtenus}"
    )


def test_le_resultat_est_exactement_le_pool_admissible_trie_par_distance(searcher):
    """
    Verification de bout en bout contre un calcul en force brute : la direction
    RETIRE des candidates, elle ne repondere rien. Si un jour le classement se
    mettait a tenir compte de l'axe, ce test le dirait.
    """
    cible = _le_moins_dense(searcher)
    delta = 0.3
    vecteurs = np.asarray(searcher.vectors, dtype=np.float32)
    positions = np.arange(len(vecteurs))
    admissibles = [
        p for p in positions
        if float(vecteurs[p][POSITION]) - float(cible.vector[POSITION]) >= delta
        and _checksums(searcher)[p] != cible.checksum
    ]
    attendu = sorted(
        admissibles,
        key=lambda p: float(np.linalg.norm(vecteurs[p] - cible.vector)),
    )[:3]
    # max_per_song=0 : le mini corpus a des titres distincts, mais le
    # dedoublonnage n'est pas le sujet ici
    trouve = searcher.search(cible, k=3, steer={AXE: delta}, max_per_song=0,
                             exclude_same_set=False, **LARGE)
    assert [searcher._position_of(m.checksum) for m in trouve] == attendu


def test_deux_axes_a_la_fois_sont_tous_les_deux_respectes(searcher):
    cible = _le_moins_dense(searcher)
    autre = "vel_p50"
    place = FEATURE_NAMES.index(autre)
    matches = searcher.search(
        cible, k=5, steer={AXE: 0.2, autre: 0.2}, **LARGE)
    assert matches
    for match in matches:
        position = searcher._position_of(match.checksum)
        assert _ecart(searcher, cible, match.checksum) >= 0.2 - 1e-6
        gap = float(searcher.vectors[position][place]) - float(cible.vector[place])
        assert gap >= 0.2 - 1e-6


# --- cas limites --------------------------------------------------------------

def test_un_axe_sans_aucune_candidate_rend_une_liste_vide(searcher):
    """
    Une liste vide est une reponse. Rendre les moins pires en pretendant qu'elles
    sont plus denses serait un mensonge de plus -- meme choix que `group_filter`.
    """
    cible = _target(searcher, 0)
    # `cs` est constant dans tout le mini corpus : personne ne peut etre au-dessus
    assert searcher.search(cible, k=5, steer={"cs": 0.3}, **LARGE) == []


def test_une_feature_deja_au_bord_de_l_ecretage_ne_promet_rien(searcher):
    """
    Les valeurs scalees sont ecretees a +/- 5 : au-dela, il n'y a pas de donnees,
    pas des donnees difficiles a trouver. Une cible deja a 5 sur un axe ne peut
    pas etre poussee plus haut, et la reponse est vide plutot que fausse.
    """
    depart = _target(searcher, 0)
    vecteur = np.array(depart.vector, dtype=np.float32)
    vecteur[POSITION] = searcher.scaler.clip
    au_bord = QueryTarget(
        vector=np.ascontiguousarray(vecteur), raw=depart.raw,
        stars=depart.stars, length_s=depart.length_s, bpm=depart.bpm,
        beatmapset_id=0, checksum="", label="cible au bord de l'ecretage",
    )
    assert searcher.search(au_bord, k=5, steer={AXE: 0.1}, **LARGE) == []
    # et l'autre sens marche encore : le bord ne bloque qu'une direction
    assert searcher.search(au_bord, k=5, steer={AXE: -0.1}, **LARGE)


def test_la_distance_affichee_reste_celle_de_la_cible(searcher):
    """
    Meme decision que pour le contre-exemple : ce qui filtre ou classe ne
    s'affiche pas. Un pourcentage de similarite qui bougerait parce qu'on a
    pousse un slider mentirait sur ce qu'il mesure.
    """
    cible = _le_moins_dense(searcher)
    matches = searcher.search(cible, k=5, steer={AXE: 0.3}, **LARGE)
    assert matches
    for match in matches:
        position = searcher._position_of(match.checksum)
        attendu = float(np.linalg.norm(
            np.asarray(searcher.vectors[position], dtype=np.float32) - cible.vector
        ))
        assert match.distance == pytest.approx(attendu, abs=1e-4)
        assert 0.0 < match.similarity <= 1.0


def test_la_direction_ne_rapproche_jamais_de_la_cible(searcher):
    """
    Filtrer une liste triee ne peut que retirer des lignes. Un resultat dirige
    plus proche que le premier resultat ordinaire signalerait que la direction
    s'applique ailleurs que sur l'admissibilite.
    """
    cible = _le_moins_dense(searcher)
    plancher = searcher.search(cible, k=1, **LARGE)[0].distance
    dirige = searcher.search(cible, k=1, steer={AXE: 0.3}, **LARGE)
    assert dirige and dirige[0].distance >= plancher - 1e-6


# --- chemin ANN ---------------------------------------------------------------

def test_l_ann_degrossit_autour_du_point_projete_pas_de_la_cible(searcher,
                                                                 monkeypatch):
    """
    Regression sur le piege annonce : au-dela d'EXACT_LIMIT candidats, c'est
    l'ANN qui pre-trie. S'il le fait autour de la cible, il rapporte surtout des
    maps du mauvais cote du seuil et l'axe est ignore. Le point sonde doit donc
    etre PROJETE sur la contrainte -- exactement le point de la contrainte le
    plus proche de la cible.
    """
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    cible = _le_moins_dense(searcher)
    delta = 0.3
    vus: list[np.ndarray] = []
    vrai = searcher.index.search

    def espion(vecteur, **kwargs):
        vus.append(np.array(vecteur, dtype=np.float32))
        return vrai(vecteur, **kwargs)

    monkeypatch.setattr(searcher.index, "search", espion)
    searcher.search(cible, k=3, steer={AXE: delta}, **LARGE)

    assert vus, "le chemin ANN doit avoir ete emprunte"
    sonde = vus[0]
    assert sonde[POSITION] == pytest.approx(
        float(cible.vector[POSITION]) + delta, abs=1e-5)
    ailleurs = [i for i in range(len(sonde)) if i != POSITION]
    assert np.allclose(sonde[ailleurs], np.asarray(cible.vector)[ailleurs])


def test_le_chemin_ann_respecte_la_marge_comme_le_chemin_exact(searcher,
                                                               monkeypatch):
    cible = _le_moins_dense(searcher)
    exact = searcher.search(cible, k=5, steer={AXE: 0.3}, **LARGE)
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    ann = searcher.search(cible, k=5, steer={AXE: 0.3}, **LARGE)
    assert [m.checksum for m in ann] == [m.checksum for m in exact]
    for match in ann:
        assert _ecart(searcher, cible, match.checksum) >= 0.3 - 1e-6


# --- combinaisons -------------------------------------------------------------

def test_la_direction_se_combine_au_contre_exemple(searcher):
    """
    Le point sonde du repoussoir est extrapole ; celui de la direction est
    projete. Les deux doivent tenir sur le meme point sans que l'un annule
    l'autre, et la marge reste une garantie, pas une preference.
    """
    cible = _le_moins_dense(searcher)
    repoussoir = searcher.target_from_checksum(
        searcher.search(cible, k=1, exclude_same_set=False, **LARGE)[0].checksum
    )
    matches = searcher.search(cible, k=5, steer={AXE: 0.3}, avoid=repoussoir,
                              exclude_same_set=False, **LARGE)
    assert matches
    for match in matches:
        assert match.checksum != repoussoir.checksum
        assert _ecart(searcher, cible, match.checksum) >= 0.3 - 1e-6


def test_la_direction_se_combine_au_filtre_par_groupe(searcher):
    cible = _le_moins_dense(searcher)
    for groupe in index_module.GROUP_NAMES:
        for match in searcher.search(cible, k=5, steer={AXE: 0.2},
                                     group_filter=groupe, **LARGE):
            assert max(match.group_share, key=match.group_share.get) == groupe
            assert _ecart(searcher, cible, match.checksum) >= 0.2 - 1e-6


def test_la_direction_se_mesure_depuis_le_centroide_pas_depuis_une_graine(searcher):
    """
    Avec plusieurs graines la reference est la map fictive : "plus dense que ce
    que j'ai demande", et non "plus dense que la premiere que j'ai citee".
    """
    colonne = np.asarray(searcher.vectors, dtype=np.float32)[:, POSITION]
    graines = [_target(searcher, int(np.argmin(colonne))),
               _target(searcher, int(np.argsort(colonne)[1]))]
    multi = combine_targets(graines)
    matches = searcher.search(multi, k=5, steer={AXE: 0.2}, **LARGE)
    assert matches
    for match in matches:
        position = searcher._position_of(match.checksum)
        gap = (float(searcher.vectors[position][POSITION])
               - float(multi.vector[POSITION]))
        assert gap >= 0.2 - 1e-6
