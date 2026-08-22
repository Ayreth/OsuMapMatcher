# -*- coding: utf-8 -*-
"""
Les trois facons de poser la question au-dela d'une seule map.

  * plusieurs graines  -- le centroide, et la bande d'etoiles qui va avec ;
  * le filtre par groupe dominant ;
  * le contre-exemple : "comme celle-ci, mais pas comme celle-la".

Le decoupage suit celui de test_scaler.py, qui teste deja une partie d'index.py
a part : un fichier par sujet plutot qu'un fichier par module.
"""

from __future__ import annotations

import numpy as np
import pytest

from osumapmatcher import index as index_module
from osumapmatcher.index import QueryTarget, Searcher, combine_targets


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


#: le petit corpus n'a ni bande d'etoiles ni duree realistes ; les filtres
#: naturels sont donc desactives partout ou ils ne sont pas le sujet
LARGE = dict(star_range=10.0, length_ratio=None)


# --- recherche multi-graines --------------------------------------------------

def test_une_seule_graine_donne_exactement_la_recherche_ordinaire(searcher):
    """
    Passer par le chemin multi ne doit rien changer quand il n'y a qu'une map :
    sinon deux facons de poser la meme question donneraient deux reponses, et
    l'interface aurait a choisir laquelle est la bonne.
    """
    direct = _target(searcher, 0)
    seule = combine_targets([direct])
    assert not seule.is_centroid
    assert seule.checksum == direct.checksum
    assert [m.checksum for m in searcher.search(seule, k=5, **LARGE)] == [
        m.checksum for m in searcher.search(direct, k=5, **LARGE)
    ]


def test_le_centroide_est_la_moyenne_des_vecteurs_scales(searcher):
    """
    La moyenne se fait dans l'espace scale, pas sur les valeurs brutes : c'est
    la que vivent les distances, et moyenner du brut redonnerait tout le poids
    aux colonnes en grandes unites -- le bug d'origine du projet.
    """
    graines = [_target(searcher, i) for i in (0, 5, 23)]
    multi = combine_targets(graines)
    assert np.allclose(multi.vector, np.mean([g.vector for g in graines], axis=0),
                       atol=1e-6)
    assert np.allclose(multi.raw, np.mean([g.raw for g in graines], axis=0),
                       rtol=1e-4)


def test_le_centroide_ne_pretend_pas_etre_une_map(searcher):
    """
    Un centroide n'a ni checksum ni beatmapset : lui en preter ferait mentir
    tout ce qui s'en sert, des exclusions aux liens osu!direct.
    """
    multi = combine_targets([_target(searcher, 0), _target(searcher, 1)])
    assert multi.is_centroid
    assert multi.checksum == ""
    assert multi.beatmapset_id == 0
    assert multi.beatmap_id == 0
    # contigu : la cible part telle quelle dans usearch sur le chemin ANN
    assert multi.vector.flags["C_CONTIGUOUS"]


def test_sans_graine_la_recherche_refuse_de_deviner(searcher):
    with pytest.raises(ValueError):
        combine_targets([])


def test_la_bande_d_etoiles_couvre_les_graines_et_pas_leur_centre(searcher):
    """
    Regression : avec des graines a 4 et 7 etoiles le centroide tombe a 5,5, et
    une bande `centre +/- range` ecarte les deux extremites -- donc exactement
    les maps qui ressemblent le plus a ce qui a ete demande. Mesure sur l'index
    reel : 22 234 candidats pour le centre contre 82 662 pour le span, et le
    meilleur resultat passe de d=1.218 a d=1.156.
    """
    graines = [_target(searcher, 0), _target(searcher, 23)]
    multi = combine_targets(graines)
    naif = QueryTarget(
        vector=multi.vector, raw=multi.raw, stars=multi.stars,
        length_s=multi.length_s, bpm=multi.bpm, beatmapset_id=0,
        checksum="", label="centre seul",
    )
    etoiles = searcher._filters[0]
    span = etoiles[searcher._candidates(
        multi, star_range=0.1, length_ratio=None, exclude_same_set=False)]
    centre = etoiles[searcher._candidates(
        naif, star_range=0.1, length_ratio=None, exclude_same_set=False)]
    for seed in multi.seeds:
        assert (np.abs(span - seed.stars) <= 0.1).any(), (
            f"aucun voisin de la graine a {seed.stars:.2f} etoiles"
        )
        assert not (np.abs(centre - seed.stars) <= 0.1).any(), (
            "le test ne prouve rien si la bande centree couvre deja les graines"
        )


def test_la_bande_de_duree_couvre_aussi_toutes_les_graines(searcher):
    """Meme raisonnement que la bande d'etoiles, pour la duree."""
    graines = [_target(searcher, 0), _target(searcher, 23)]
    multi = combine_targets(graines)
    assert multi.length_span == (
        min(g.length_s for g in graines), max(g.length_s for g in graines)
    )
    durees = searcher._filters[1][searcher._candidates(
        multi, star_range=10.0, length_ratio=(0.9, 1.1), exclude_same_set=False)]
    assert durees.min() <= multi.length_span[0] * 1.1
    assert durees.max() >= multi.length_span[1] * 0.9


def test_les_beatmapsets_et_les_graines_sont_tous_exclus(searcher):
    """
    `exclude_same_set` ne connaissait qu'un beatmapset. Avec N graines il faut
    exclure les N, et les graines elles-memes : se voir recommander ce qu'on
    vient de donner en exemple n'est une recommandation pour personne.
    """
    graines = [_target(searcher, i) for i in (0, 3, 7)]
    multi = combine_targets(graines)
    assert multi.excluded_sets == tuple(sorted(g.beatmapset_id for g in graines))
    assert multi.excluded_checksums == tuple(sorted(g.checksum for g in graines))

    candidats = searcher._candidates(
        multi, star_range=10.0, length_ratio=None, exclude_same_set=True
    ).tolist()
    for graine in graines:
        assert searcher._position_of(graine.checksum) not in candidats
    sets = searcher._filters[2]
    assert not np.isin(sets[candidats], [g.beatmapset_id for g in graines]).any()


def test_le_centroide_cherche_bien_par_le_chemin_ann(searcher, monkeypatch):
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    multi = combine_targets([_target(searcher, 0), _target(searcher, 23)])
    assert searcher.search(multi, k=3, **LARGE)


# --- filtre par groupe dominant -----------------------------------------------

def test_le_filtre_par_groupe_ne_rend_que_le_groupe_demande(searcher):
    target = _target(searcher, 0)
    for groupe in index_module.GROUP_NAMES:
        for match in searcher.search(target, k=5, group_filter=groupe, **LARGE):
            assert max(match.group_share, key=match.group_share.get) == groupe


def test_le_filtre_par_groupe_remplit_la_liste_au_lieu_de_la_rogner(searcher):
    """
    Regression : filtrer les k resultats deja calcules rendrait une liste
    presque vide. C'est le piege du dedoublonnage par chanson, qui prend large
    avant de couper -- le filtre doit s'appliquer a l'ORDRE, pas au resultat.
    """
    target = _target(searcher, 0)
    base = searcher.search(target, k=3, **LARGE)
    dominants = {max(m.group_share, key=m.group_share.get) for m in base}
    absent = next(g for g in ("aim", "spacing") if g not in dominants)

    filtre = searcher.search(target, k=3, group_filter=absent, **LARGE)
    assert len(filtre) == 3, (
        f"le groupe {absent} n'apparait pas dans les 3 premiers resultats : "
        "un filtre applique apres coup en rendrait zero"
    )


def test_le_filtre_par_groupe_ne_rapproche_jamais_de_la_cible(searcher):
    """
    Filtrer une liste triee ne peut que retirer des lignes. Un resultat filtre
    plus proche que le premier resultat non filtre signalerait que le filtre
    s'applique ailleurs que sur l'ordre.
    """
    target = _target(searcher, 0)
    plancher = searcher.search(target, k=1, **LARGE)[0].distance
    for groupe in index_module.GROUP_NAMES:
        trouve = searcher.search(target, k=3, group_filter=groupe, **LARGE)
        if trouve:
            assert trouve[0].distance >= plancher - 1e-6


def test_un_groupe_sans_aucun_candidat_rend_une_liste_vide(searcher):
    """Une liste vide est une reponse ; une exception n'en serait pas une."""
    target = _target(searcher, 0)
    vides = [
        groupe for groupe in index_module.GROUP_NAMES
        if not searcher.search(target, k=5, group_filter=groupe, **LARGE)
    ]
    assert vides, "le corpus de test doit contenir un groupe sans candidat"


def test_un_groupe_de_filtrage_inconnu_est_refuse(searcher):
    with pytest.raises(KeyError):
        searcher.search(_target(searcher, 0), k=3, group_filter="nawak")


def test_le_filtre_par_groupe_marche_aussi_par_le_chemin_ann(searcher, monkeypatch):
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    matches = searcher.search(
        _target(searcher, 0), k=3, group_filter="spacing", **LARGE
    )
    assert matches
    for match in matches:
        assert max(match.group_share, key=match.group_share.get) == "spacing"


# --- contre-exemple -----------------------------------------------------------

def test_le_repoussoir_n_est_jamais_dans_ses_propres_resultats(searcher):
    """
    Regression : la penalite ne suffit pas a l'ecarter. Le repoussoir est a
    distance nulle de lui-meme, donc c'est lui que la penalite epargne le plus.
    Mesure sur l'index reel : a beta=0,15 il ressortait quatrieme.
    """
    target = _target(searcher, 0)
    sans_set = dict(exclude_same_set=False, **LARGE)
    base = searcher.search(target, k=5, **sans_set)
    repoussoir = searcher.target_from_checksum(base[0].checksum)
    assert base[0].checksum == repoussoir.checksum, "sans avoid il est bien la"

    # exclude_same_set=False : seule l'exclusion par checksum peut le retirer
    avec = searcher.search(target, k=5, avoid=repoussoir, **sans_set)
    assert all(match.checksum != repoussoir.checksum for match in avec)


def test_un_repoussoir_egal_a_la_cible_ne_change_pas_l_ordre(searcher):
    """
    Cas degenere mais atteignable depuis l'interface. Le score devient
    `(1 - beta) * distance^2`, un multiple positif : le classement ne bouge pas,
    et c'est le seul comportement defendable -- fuir ce qu'on demande n'a pas
    de sens.
    """
    target = _target(searcher, 0)
    sans = searcher.search(target, k=5, **LARGE)
    avec = searcher.search(target, k=5, avoid=target, **LARGE)
    assert [m.checksum for m in avec] == [m.checksum for m in sans]


def test_une_force_de_repulsion_nulle_ne_change_que_l_exclusion(searcher):
    target = _target(searcher, 0)
    repoussoir = _target(searcher, 12)
    sans_set = dict(exclude_same_set=False, **LARGE)
    sans = [m.checksum for m in searcher.search(target, k=5, **sans_set)]
    avec = [m.checksum for m in searcher.search(
        target, k=5, avoid=repoussoir, avoid_strength=0.0, **sans_set)]
    attendu = [c for c in sans if c != repoussoir.checksum]
    assert avec[: len(attendu)] == attendu[: len(avec)]


@pytest.mark.parametrize("force", [-0.01, 0.9, 1.0])
def test_une_force_de_repulsion_hors_bornes_est_refusee(searcher, force):
    """
    Au-dela de MAX_AVOID_STRENGTH la penalite equivaut a chercher autour d'un
    point extrapole tres loin des donnees : mesure a beta=0,90, le premier
    resultat sortait a 3,34 de la cible la ou la recherche ordinaire trouve 1,11.
    """
    with pytest.raises(ValueError):
        searcher.search(_target(searcher, 0), k=3, avoid=_target(searcher, 12),
                        avoid_strength=force)


def test_le_contre_exemple_eloigne_bien_du_repoussoir(searcher):
    target = _target(searcher, 0)
    sans_set = dict(exclude_same_set=False, **LARGE)
    repoussoir = searcher.target_from_checksum(
        searcher.search(target, k=1, **sans_set)[0].checksum
    )

    def eloignement(matches) -> float:
        vecteur = np.asarray(repoussoir.vector, dtype=np.float32)
        return float(np.mean([
            np.linalg.norm(
                np.asarray(searcher.vectors[searcher._position_of(m.checksum)],
                           dtype=np.float32) - vecteur
            )
            for m in matches
        ]))

    sans = searcher.search(target, k=5, **sans_set)
    avec = searcher.search(target, k=5, avoid=repoussoir, **sans_set)
    assert eloignement(avec) > eloignement(sans)


def test_la_distance_affichee_reste_celle_de_la_cible(searcher):
    """
    Regression : le score penalise CLASSE, il ne s'affiche pas. Un pourcentage
    de similarite qui bougerait parce qu'on a nomme un repoussoir mentirait sur
    ce qu'il mesure -- et le score, lui, peut etre negatif.
    """
    target = _target(searcher, 0)
    repoussoir = _target(searcher, 12)
    matches = searcher.search(target, k=5, avoid=repoussoir,
                              exclude_same_set=False, **LARGE)
    assert matches
    for match in matches:
        position = searcher._position_of(match.checksum)
        attendu = float(np.linalg.norm(
            np.asarray(searcher.vectors[position], dtype=np.float32) - target.vector
        ))
        assert match.distance == pytest.approx(attendu, abs=1e-4)
        assert 0.0 < match.similarity <= 1.0


def test_le_contre_exemple_marche_aussi_par_le_chemin_ann(searcher, monkeypatch):
    """
    Le chemin ANN degrossit autour du point extrapole, pas de la cible : c'est
    ce point-la que le score penalise ordonne. Il doit donc arriver contigu dans
    usearch, comme tout ce qui y entre.
    """
    monkeypatch.setattr(index_module, "EXACT_LIMIT", 0)
    matches = searcher.search(
        _target(searcher, 0), k=3, avoid=_target(searcher, 12),
        exclude_same_set=False, **LARGE,
    )
    assert matches


def test_le_contre_exemple_se_combine_au_centroide_et_au_filtre(searcher):
    """Les trois chantiers doivent tenir ensemble, pas seulement separement."""
    multi = combine_targets([_target(searcher, 0), _target(searcher, 23)])
    matches = searcher.search(
        multi, k=3, group_filter="spacing", avoid=_target(searcher, 12), **LARGE
    )
    for match in matches:
        assert max(match.group_share, key=match.group_share.get) == "spacing"
        assert match.checksum not in multi.excluded_checksums
