# -*- coding: utf-8 -*-
"""
Tests du pool non classe : recuperation, integration a l'index, filtrage.

Rien ici ne touche le reseau. `_get` est remplace par un dictionnaire, ce qui
permet de tester ce qui compte vraiment -- la verification du md5 -- sans
dependre d'un mirror qui pourrait tomber ou changer.

Le cas garde le plus jalousement : un mirror qui sert autre chose que ce qu'il
annonce. Tout l'index est cle sur le checksum, qui EST le md5 du .osu ; une
entree dont le md5 ne correspond pas serait invisible depuis le jeu, et elle le
serait silencieusement.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from osumapmatcher import unranked as unranked_module
from osumapmatcher.index import DB_FILE, DUMP_STATUS, Searcher
from osumapmatcher.unranked import (
    DEFAULT_POOL,
    MIRRORS,
    BeatmapRef,
    download_refs,
    load_annotations,
    refs_from_sets,
    resolve_mirrors,
    write_manifest,
)


def _set(set_id: int, plays: int, beatmaps: list[dict], status: str = "graveyard") -> dict:
    return {
        "id": set_id,
        "play_count": plays,
        "status": status,
        "artist": f"Artist {set_id}",
        "title": f"Title {set_id}",
        "beatmaps": beatmaps,
    }


def _beatmap(bid: int, mode: int = 0, checksum: str | None = None, **extra) -> dict:
    payload = {
        "id": bid,
        "mode_int": mode,
        "checksum": checksum if checksum is not None else f"{bid:032x}",
        "version": f"Diff {bid}",
        "difficulty_rating": 5.5,
    }
    payload.update(extra)
    return payload


# --- aplatissement des sets ---------------------------------------------------

def test_refs_ne_gardent_que_le_std():
    sets = [_set(1, 100, [_beatmap(10, mode=0), _beatmap(11, mode=3),
                          _beatmap(12, mode=1)])]
    refs = refs_from_sets(sets)
    assert [r.beatmap_id for r in refs] == [10]


def test_refs_acceptent_le_mode_en_texte():
    """osu.direct renvoie `mode_int`, d'autres sources un `mode` textuel."""
    sets = [_set(1, 100, [{"id": 10, "mode": "osu", "checksum": "a" * 32,
                           "version": "X", "difficulty_rating": 4.0},
                          {"id": 11, "mode": "mania", "checksum": "b" * 32,
                           "version": "Y", "difficulty_rating": 4.0}])]
    assert [r.beatmap_id for r in refs_from_sets(sets)] == [10]


def test_refs_jettent_ce_qui_n_a_pas_de_md5():
    """Sans md5 annonce, le telechargement ne serait pas verifiable."""
    sets = [_set(1, 100, [_beatmap(10, checksum=""), _beatmap(11)])]
    assert [r.beatmap_id for r in refs_from_sets(sets)] == [11]


def test_refs_classees_par_plays_decroissants():
    sets = [_set(1, 100, [_beatmap(10)]), _set(2, 9000, [_beatmap(20)]),
            _set(3, 500, [_beatmap(30)])]
    refs = refs_from_sets(sets)
    assert [r.beatmap_id for r in refs] == [20, 30, 10]
    assert [r.playcount for r in refs] == [9000, 500, 100]


def test_refs_dedoublonnees_par_beatmap_id():
    """Un meme set peut revenir de deux statuts pagines separement."""
    sets = [_set(1, 100, [_beatmap(10)]), _set(1, 100, [_beatmap(10)])]
    assert len(refs_from_sets(sets)) == 1


def test_le_statut_de_la_difficulte_prime_sur_celui_du_set():
    sets = [_set(1, 100, [_beatmap(10, status="wip")], status="graveyard")]
    assert refs_from_sets(sets)[0].status == "wip"


def test_le_nom_de_fichier_suit_la_convention_du_dump():
    """`build_corpus` recupere l'identifiant depuis le nom de fichier."""
    ref = refs_from_sets([_set(1, 100, [_beatmap(4242)])])[0]
    assert ref.filename == "4242.osu"


# --- telechargement -----------------------------------------------------------

@pytest.fixture
def fake_network(monkeypatch):
    """Remplace le reseau par un dict url -> octets, et compte les appels."""
    served: dict[str, bytes] = {}
    calls: list[str] = []

    def fake_get(url: str, *, timeout: float = 30.0) -> bytes:
        calls.append(url)
        if url not in served:
            raise OSError(f"404 simule sur {url}")
        return served[url]

    monkeypatch.setattr(unranked_module, "_get", fake_get)
    monkeypatch.setattr(unranked_module.time, "sleep", lambda _s: None)
    return served, calls


def _ref(bid: int, payload: bytes, **extra) -> BeatmapRef:
    fields = {
        "beatmap_id": bid, "beatmapset_id": 1,
        "checksum": hashlib.md5(payload).hexdigest(),
        "status": "graveyard", "playcount": 1000, "stars": 5.0,
        "label": f"map {bid}",
    }
    fields.update(extra)
    return BeatmapRef(**fields)


def test_telechargement_ecrit_et_verifie(tmp_path, fake_network):
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    payload = b"osu file format v14\n"
    ref = _ref(10, payload)
    served[mirror.file_url(10)] = payload

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert report.n_downloaded == 1
    assert report.n_checksum_mismatch == 0
    assert (tmp_path / "10.osu").read_bytes() == payload


def test_un_md5_qui_ne_colle_pas_est_rejete(tmp_path, fake_network):
    """
    Le coeur du sujet. Un mirror qui repackage (fins de ligne normalisees,
    audio renomme) produirait une entree que le jeu ne reconnaitrait jamais.
    Mieux vaut perdre la map que l'indexer sous un mauvais checksum.
    """
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    ref = _ref(10, b"ce que le mirror annonce")
    served[mirror.file_url(10)] = b"ce que le mirror sert vraiment"

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert report.n_checksum_mismatch == 1
    assert report.n_downloaded == 0
    assert not (tmp_path / "10.osu").exists()
    assert ref.beatmap_id in report.failures


def test_la_reprise_ne_retelecharge_pas(tmp_path, fake_network):
    served, calls = fake_network
    mirror = MIRRORS["osu.direct"]
    payload = b"osu file format v14\n"
    ref = _ref(10, payload)
    served[mirror.file_url(10)] = payload

    first = download_refs([ref], tmp_path, mirrors=mirror, progress=False)
    calls.clear()
    second = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert first.n_downloaded == 1
    assert second.n_reused == 1 and second.n_downloaded == 0
    assert calls == [], "un fichier deja bon ne doit declencher aucun appel"


def test_un_fichier_present_mais_perime_est_repris(tmp_path, fake_network):
    """Une map graveyard reuploadee change de md5 : il faut la reprendre."""
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    nouveau = b"version 2 de la map\n"
    ref = _ref(10, nouveau)
    served[mirror.file_url(10)] = nouveau
    (tmp_path / "10.osu").write_bytes(b"version 1, perimee\n")

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert report.n_downloaded == 1
    assert (tmp_path / "10.osu").read_bytes() == nouveau


def test_le_rattrapage_recupere_un_echec_transitoire(tmp_path, monkeypatch):
    """
    Mesure sur un lot reel de 183 maps : 8 tombent a 5 req/s alors qu'elles
    repondent parfaitement en isole. C'est le debit qui les fait tomber, pas
    leur contenu. Sans passe de rattrapage, on perdrait 4 % du pool pour rien.
    """
    mirror = MIRRORS["osu.direct"]
    payload = b"osu file format v14\n"
    ref = _ref(10, payload)
    tentatives = {"n": 0}

    def flaky_get(url: str, *, timeout: float = 30.0) -> bytes:
        tentatives["n"] += 1
        if tentatives["n"] == 1:
            raise OSError("throttle simule")
        return payload

    monkeypatch.setattr(unranked_module, "_get", flaky_get)
    monkeypatch.setattr(unranked_module.time, "sleep", lambda _s: None)

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert report.n_downloaded == 1
    assert report.n_failed == 0
    assert report.failures == []
    assert (tmp_path / "10.osu").read_bytes() == payload


def test_un_md5_discordant_n_est_pas_rejoue_sur_le_meme_mirror(tmp_path, fake_network):
    """Rejouer sur celui qui vient de servir les mauvais octets est inutile."""
    served, calls = fake_network
    mirror = MIRRORS["osu.direct"]
    ref = _ref(10, b"annonce")
    served[mirror.file_url(10)] = b"servi"

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)

    assert report.n_checksum_mismatch == 1
    assert len(calls) == 1, "un seul mirror : pas de seconde tentative"


def test_un_md5_discordant_bascule_sur_un_autre_mirror(tmp_path, fake_network):
    """
    Un mirror peut servir une version perimee alors qu'un autre a la bonne.
    C'est tout l'interet d'en interroger plusieurs : la bascule recupere la map
    au lieu de la perdre, et le md5 reste la seule chose a laquelle on se fie.
    """
    served, _ = fake_network
    bon = b"la bonne version\n"
    ref = _ref(10, bon)
    served[MIRRORS["osu.direct"].file_url(10)] = b"une version perimee\n"
    served[MIRRORS["catboy"].file_url(10)] = bon

    report = download_refs(
        [ref], tmp_path, mirrors=["osu.direct", "catboy"], progress=False
    )

    assert report.n_downloaded == 1
    assert report.n_checksum_mismatch == 0
    assert report.failures == []
    assert (tmp_path / "10.osu").read_bytes() == bon


def test_la_bascule_evite_le_mirror_fautif(tmp_path, fake_network):
    """Un mirror qui n'a pas le fichier ne doit pas etre resollicite pour lui."""
    served, calls = fake_network
    bon = b"osu file format v14\n"
    ref = _ref(10, bon)
    served[MIRRORS["catboy"].file_url(10)] = bon   # osu.direct ne l'a pas

    report = download_refs(
        [ref], tmp_path, mirrors=["osu.direct", "catboy"], progress=False
    )

    assert report.n_downloaded == 1
    assert calls.count(MIRRORS["osu.direct"].file_url(10)) == 1, (
        "le mirror fautif ne doit etre essaye qu'une fois"
    )


def test_le_rattrapage_ne_boucle_pas_quand_un_mirror_est_muet(tmp_path, fake_network):
    """
    Regression. Premiere version : la passe de rattrapage remettait dans une
    file partagee les maps qu'un worker devait eviter. Quand l'autre thread
    avait fini de tirer, celui qui restait les remettait indefiniment -- le job
    ne se terminait jamais. Les lots sont maintenant assignes en amont.

    Sans le correctif, ce test ne rend pas la main.
    """
    served, _ = fake_network
    refs = []
    for bid in range(10, 25):
        payload = f"map {bid}\n".encode()
        refs.append(_ref(bid, payload))
        # seul catboy a les fichiers : osu.direct echouera sur toutes
        served[MIRRORS["catboy"].file_url(bid)] = payload

    report = download_refs(
        refs, tmp_path, mirrors=["osu.direct", "catboy"], progress=False
    )

    assert report.n_downloaded == len(refs)
    assert report.n_failed == 0


def test_le_pool_repartit_le_travail(tmp_path, fake_network):
    """Trois mirrors, un thread chacun : tous doivent servir quelque chose."""
    served, calls = fake_network
    refs = []
    for bid in range(10, 40):
        payload = f"map {bid}\n".encode()
        refs.append(_ref(bid, payload))
        for nom in DEFAULT_POOL:
            served[MIRRORS[nom].file_url(bid)] = payload

    report = download_refs(refs, tmp_path, progress=False)

    assert report.n_downloaded == len(refs)
    assert report.mirrors == list(DEFAULT_POOL)
    servis = {nom for nom in DEFAULT_POOL
              if any(MIRRORS[nom].osu_url.split("{")[0] in c for c in calls)}
    assert servis == set(DEFAULT_POOL), f"seuls {servis} ont travaille"


def test_resolve_mirrors_accepte_les_trois_formes():
    assert [m.name for m in resolve_mirrors(None)] == list(DEFAULT_POOL)
    assert [m.name for m in resolve_mirrors(MIRRORS["catboy"])] == ["catboy"]
    assert [m.name for m in resolve_mirrors(["catboy", "ppy"])] == ["catboy", "ppy"]
    # les doublons ne doivent pas fabriquer deux threads sur le meme hote
    assert [m.name for m in resolve_mirrors(["catboy", "catboy"])] == ["catboy"]
    with pytest.raises(KeyError):
        resolve_mirrors(["nawak"])


def test_le_rattrapage_abandonne_apres_une_passe(tmp_path, fake_network):
    """Une map durablement absente ne doit pas boucler."""
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    report = download_refs([_ref(10, b"jamais servi")], tmp_path,
                           mirrors=mirror, progress=False)
    assert report.n_failed == 1
    assert report.failures == [10]


def test_un_echec_reseau_ne_stoppe_pas_le_lot(tmp_path, fake_network):
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    bon = b"osu file format v14\n"
    refs = [_ref(10, b"absent du mirror"), _ref(11, bon)]
    served[mirror.file_url(11)] = bon

    report = download_refs(refs, tmp_path, mirrors=mirror, progress=False)

    assert report.n_failed == 1
    assert report.n_downloaded == 1
    assert (tmp_path / "11.osu").exists()


# --- manifeste ----------------------------------------------------------------

def test_le_manifeste_exclut_les_maps_ecartees(tmp_path, fake_network):
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    bon = b"osu file format v14\n"
    refs = [_ref(10, b"jamais servi"), _ref(11, bon)]
    served[mirror.file_url(11)] = bon

    report = download_refs(refs, tmp_path, mirrors=mirror, progress=False)
    write_manifest(tmp_path, refs, mirror=mirror, statuses=["graveyard"],
                   report=report)

    annotations = load_annotations(tmp_path)
    attendu = hashlib.md5(bon).hexdigest()
    assert set(annotations) == {attendu}, "une map absente du disque n'a rien a annoter"
    assert annotations[attendu]["status"] == "graveyard"
    assert annotations[attendu]["playcount"] == 1000
    assert annotations[attendu]["beatmap_id"] == 11


def test_annotation_jointe_sur_le_checksum_pas_le_beatmap_id(tmp_path, fake_network):
    """
    Rencontre en vrai sur le lot de test : `3066127.osu` porte
    `BeatmapID:3155717` dans ses metadonnees, parce que son auteur a copie une
    difficulte d'ailleurs sans nettoyer. Joindre sur le beatmap_id etiquetait la
    map comme venant du dump, en silence. Le checksum, lui, ne peut pas mentir.
    """
    served, _ = fake_network
    mirror = MIRRORS["osu.direct"]
    payload = b"osu file format v14\nBeatmapID:3155717\n"
    ref = _ref(3066127, payload)
    served[mirror.file_url(3066127)] = payload

    report = download_refs([ref], tmp_path, mirrors=mirror, progress=False)
    write_manifest(tmp_path, [ref], mirror=mirror, statuses=["graveyard"],
                   report=report)

    annotations = load_annotations(tmp_path)
    md5_reel = hashlib.md5(payload).hexdigest()
    assert md5_reel in annotations
    assert annotations[md5_reel]["status"] == "graveyard"
    # et surtout : la cle n'est pas un identifiant que le fichier peut trahir
    assert 3155717 not in annotations and 3066127 not in annotations


def test_manifeste_absent_donne_un_dict_vide(tmp_path):
    """Construire un index sans pool non classe doit rester sans condition."""
    assert load_annotations(tmp_path) == {}
    assert load_annotations(tmp_path / "rien.json") == {}


def test_le_manifeste_date_la_recuperation(tmp_path, fake_network):
    """Une map graveyard peut etre reuploadee : savoir quand on l'a prise
    est la seule facon de reperer un pool perime."""
    mirror = MIRRORS["osu.direct"]
    refs = [_ref(10, b"x")]
    report = download_refs([], tmp_path, mirrors=mirror, progress=False)
    path = write_manifest(tmp_path, refs, mirror=mirror,
                          statuses=["graveyard"], report=report)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["fetched_at"].endswith("Z")
    assert payload["mirror"] == "osu.direct"


# --- sources ------------------------------------------------------------------

def test_seul_osu_direct_sait_classer():
    """
    Verifie a la main : la recherche d'hinamizawa est au format Cheesegull, qui
    n'a pas de champ playcount, et le `sort` de catboy ne trie pas. Si ca
    change un jour, ce test le dira.
    """
    assert MIRRORS["osu.direct"].can_rank
    assert not MIRRORS["hinamizawa"].can_rank
    assert not MIRRORS["catboy"].can_rank
    assert not MIRRORS["ppy"].can_rank


def test_classer_sans_source_capable_est_une_erreur_claire():
    with pytest.raises(ValueError, match="osu.direct"):
        unranked_module.list_top_sets(10, mirror=MIRRORS["hinamizawa"])


# --- integration a l'index ----------------------------------------------------

def _index_avec_statuts(source: Path, destination: Path, marquees: dict) -> Path:
    """Copie un index et marque quelques positions comme non classees."""
    shutil.copytree(source, destination)
    connection = sqlite3.connect(destination / DB_FILE)
    try:
        for position, (status, plays) in marquees.items():
            connection.execute(
                "UPDATE maps SET status = ?, playcount = ? WHERE pos = ?",
                (status, plays, position),
            )
        connection.commit()
    finally:
        connection.close()
    return destination


def test_les_maps_du_dump_sont_marquees_dump(mini_index):
    searcher = Searcher(mini_index / "nomod")
    try:
        assert searcher.has_status
        row = searcher.db.execute("SELECT status FROM maps LIMIT 1").fetchone()
        assert row["status"] == DUMP_STATUS
    finally:
        searcher.close()


def test_allow_unranked_false_retire_les_non_classees(mini_index, tmp_path):
    marquees = {position: ("graveyard", 500_000) for position in range(0, 24, 2)}
    directory = _index_avec_statuts(
        mini_index / "nomod", tmp_path / "marque", marquees
    )
    searcher = Searcher(directory)
    try:
        target = searcher.target_from_checksum(
            searcher.db.execute(
                "SELECT checksum FROM maps WHERE pos = 1"
            ).fetchone()["checksum"]
        )
        large = dict(k=50, star_range=10.0, length_ratio=None,
                     exclude_same_set=False, max_per_song=0)

        avec = searcher.search(target, allow_unranked=True, **large)
        sans = searcher.search(target, allow_unranked=False, **large)

        assert len(avec) > len(sans), "le filtre doit reellement retirer des maps"
        assert any(m.is_unranked for m in avec)
        assert not any(m.is_unranked for m in sans)
        assert all(m.status == DUMP_STATUS for m in sans)
    finally:
        searcher.close()


def test_le_playcount_remonte_jusqu_au_resultat(mini_index, tmp_path):
    directory = _index_avec_statuts(
        mini_index / "nomod", tmp_path / "plays", {3: ("graveyard", 34_443_737)}
    )
    searcher = Searcher(directory)
    try:
        checksum = searcher.db.execute(
            "SELECT checksum FROM maps WHERE pos = 3"
        ).fetchone()["checksum"]
        target = searcher.target_from_checksum(
            searcher.db.execute(
                "SELECT checksum FROM maps WHERE pos = 0"
            ).fetchone()["checksum"]
        )
        matches = searcher.search(
            target, k=50, star_range=10.0, length_ratio=None,
            exclude_same_set=False, max_per_song=0,
        )
        trouvee = next(m for m in matches if m.checksum == checksum)
        assert trouvee.playcount == 34_443_737
        assert trouvee.is_unranked
        assert trouvee.as_dict()["is_unranked"] is True
    finally:
        searcher.close()


def test_un_index_sans_colonne_status_reste_lisible(mini_index, tmp_path):
    """
    Le launcher installe un index prebati telecharge depuis une release. Mettre
    le code a jour ne doit pas obliger a retelecharger 165 Mo.
    """
    directory = tmp_path / "ancien"
    shutil.copytree(mini_index / "nomod", directory)
    connection = sqlite3.connect(directory / DB_FILE)
    try:
        # le schema d'avant l'ajout du pool non classe, ecrit tel quel
        connection.executescript("""
            CREATE TABLE maps_old (
                pos INTEGER PRIMARY KEY, checksum TEXT NOT NULL UNIQUE,
                beatmap_id INTEGER NOT NULL, beatmapset_id INTEGER NOT NULL,
                artist TEXT, title TEXT, creator TEXT, version TEXT,
                stars REAL, bpm REAL, length_s REAL, n_objects INTEGER
            );
            INSERT INTO maps_old SELECT pos, checksum, beatmap_id, beatmapset_id,
                artist, title, creator, version, stars, bpm, length_s, n_objects
                FROM maps;
            DROP TABLE maps;
            ALTER TABLE maps_old RENAME TO maps;
        """)
        connection.commit()
    finally:
        connection.close()

    searcher = Searcher(directory)
    try:
        assert not searcher.has_status
        target = searcher.target_from_checksum(
            searcher.db.execute(
                "SELECT checksum FROM maps WHERE pos = 0"
            ).fetchone()["checksum"]
        )
        matches = searcher.search(target, k=5, star_range=10.0, length_ratio=None)
        assert matches, "un ancien index doit continuer a repondre"
        assert all(m.status == DUMP_STATUS for m in matches)
        assert all(m.playcount is None for m in matches)
        # et le filtre ne doit pas tout vider faute de colonne
        assert searcher.search(
            target, k=5, star_range=10.0, length_ratio=None, allow_unranked=False
        )
    finally:
        searcher.close()


def test_build_fusionne_deux_corpus(tmp_path, mini_corpus):
    """Deux dossiers de tranches en entree, un seul index en sortie."""
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]
    coupure = 16

    def _ecrire(directory, rows):
        directory.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(rows, columns=COLUMNS)
        frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
        frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    _ecrire(tmp_path / "dump", features[:coupure])
    _ecrire(tmp_path / "grave", features[coupure:])

    annotations = {
        f["checksum"]: {"status": "graveyard", "playcount": 12_345}
        for f in features[coupure:]
    }
    report = build(
        [tmp_path / "dump", tmp_path / "grave"], tmp_path / "index",
        annotations=annotations,
    )

    assert report["n_maps"] == len(features)
    assert report["n_unranked"] == len(features) - coupure
    assert report["n_sources"] == 2

    connection = sqlite3.connect(tmp_path / "index" / DB_FILE)
    try:
        connection.row_factory = sqlite3.Row
        compte = connection.execute(
            "SELECT status, COUNT(*) n FROM maps GROUP BY status"
        ).fetchall()
        par_statut = {row["status"]: row["n"] for row in compte}
    finally:
        connection.close()
    assert par_statut == {DUMP_STATUS: coupure, "graveyard": len(features) - coupure}


def test_build_accepte_toujours_un_seul_dossier(tmp_path, mini_corpus):
    """La signature reste compatible avec les appels existants."""
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]
    directory = tmp_path / "corpus"
    directory.mkdir()
    frame = pd.DataFrame(features, columns=COLUMNS)
    frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
    frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    report = build(directory, tmp_path / "index")
    assert report["n_maps"] == len(features)
    assert report["n_unranked"] == 0


# --- pieges qui ont mordu en vrai ---------------------------------------------

def test_le_pool_non_classe_n_est_jamais_pris_pour_le_dump(tmp_path, monkeypatch):
    """
    Regression, payee cher. Le dossier par defaut s'appelait
    `unranked_osu_files`, or `find_dump` cherche les dossiers dont le nom
    contient `osu_files`. Il scanne `data/` avant la racine, le comptage est
    plafonne, et la comparaison est un `>` strict : a egalite le pool gagnait.
    Tout le corpus principal s'est retrouve construit dessus, en silence.
    """
    from osumapmatcher import paths

    monkeypatch.setenv("OSUMAPMATCHER_DATA", str(tmp_path))
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    # plafond de comptage abaisse : c'est l'egalite AU PLAFOND qui declenchait
    # le bug, les deux dossiers etant plus fournis que lui
    monkeypatch.setattr(paths, "_COUNT_CAP", 3)

    dump = tmp_path / "2026_01_01_osu_files"
    pool = tmp_path / "unranked_osu_files"
    for directory in (dump, pool):
        directory.mkdir(parents=True)
    for i in range(5):
        (dump / f"{i}.osu").write_text("osu file format v14\n", encoding="utf-8")
    # le pool en contient PLUS que le dump : seul le manifeste peut trancher
    for i in range(50):
        (pool / f"{1000 + i}.osu").write_text("osu file format v14\n", encoding="utf-8")
    (pool / "manifest.json").write_text('{"beatmaps": {}}', encoding="utf-8")

    assert paths.is_unranked_dir(pool)
    assert not paths.is_unranked_dir(dump)
    assert paths.find_dump() == dump, "find_dump doit ignorer le pool non classe"


def test_la_reprise_refuse_une_source_differente(tmp_path):
    """
    Regression. `build_corpus` enregistrait sa source dans son manifeste mais
    ne la relisait jamais : relance sur un autre dossier, il reutilisait ses
    vieilles tranches et rapportait leurs stats contre le nouveau nombre de
    fichiers. Seul symptome visible : des taux de skip au-dessus de 100 %.
    """
    from osumapmatcher.corpus import resume_is_valid

    out = tmp_path / "corpus"
    out.mkdir()
    (out / "manifest.json").write_text(
        json.dumps({"source": str(tmp_path / "dump"), "mods": ""}), encoding="utf-8"
    )

    assert resume_is_valid(out, tmp_path / "dump", None)
    assert not resume_is_valid(out, tmp_path / "autre_dossier", None)
    assert not resume_is_valid(out, tmp_path / "dump", "DT"), "les mods comptent aussi"
    # sans manifeste il n'y a rien a contredire
    assert resume_is_valid(tmp_path / "vierge", tmp_path / "dump", None)


def test_la_reprise_refuse_un_manifeste_qui_se_contredit(tmp_path):
    """
    Le cas qui a reellement mordu, et que ni la source ni le nombre de fichiers
    n'attrapaient : le dossier source avait grossi (3 616 -> 9 958 fichiers),
    la source enregistree etait la bonne, `n_seen` aussi -- mais la tranche
    reutilisee ne couvrait que l'ancien contenu.

    Le manifeste se trahit tout seul : chaque fichier vu est garde ou skippe,
    donc `n_kept + skips == n_seen`. Ici 3321 + 295 = 3616, pas 9958.
    """
    from osumapmatcher.corpus import resume_is_valid

    out = tmp_path / "corpus"
    out.mkdir()
    source = tmp_path / "pool"

    def _ecrire(n_seen, n_kept, skips):
        (out / "manifest.json").write_text(
            json.dumps({"source": str(source), "mods": "",
                        "n_seen": n_seen, "n_kept": n_kept, "skips": skips}),
            encoding="utf-8",
        )

    _ecrire(9958, 3321, {"too_few_objects": 58, "too_short": 231, "no_hitobjects": 6})
    assert not resume_is_valid(out, source, None), (
        "3321 + 295 != 9958 : la tranche ne decrit pas ce lot"
    )

    _ecrire(3616, 3321, {"too_few_objects": 58, "too_short": 231, "no_hitobjects": 6})
    assert resume_is_valid(out, source, None), "un manifeste coherent doit passer"


def test_les_tranches_orphelines_sont_effacees(tmp_path, mini_corpus):
    """
    Ignorer les vieilles tranches ne suffit pas : `load_corpus` ramasse tous
    les `chunk_*.parquet` du dossier, donc un lot plus petit que le precedent
    laisserait des orphelines que l'index avalerait sans un mot.
    """
    pytest.importorskip("pandas")
    from osumapmatcher.corpus import build_corpus

    source = tmp_path / "src"
    source.mkdir()
    for raw, features in mini_corpus[:3]:
        (source / f"{int(features['beatmap_id'])}.osu").write_bytes(raw)

    out = tmp_path / "corpus"
    out.mkdir()
    # tranches d'un lot precedent, sur une autre source
    (out / "chunk_00000.parquet").write_bytes(b"perime")
    (out / "chunk_00007.parquet").write_bytes(b"perime")
    (out / "chunk_00007.stats.json").write_text('{"n_rows": 9, "skips": {}}',
                                                encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps({"source": str(tmp_path / "ailleurs"), "mods": ""}), encoding="utf-8"
    )

    build_corpus(source, out, workers=1, progress=False)

    restantes = sorted(p.name for p in out.glob("chunk_*.parquet"))
    assert restantes == ["chunk_00000.parquet"], f"orphelines restantes : {restantes}"


def test_une_map_du_dump_n_est_pas_reetiquetee(tmp_path, mini_corpus):
    """
    Le dump fait autorite sur "cette map est ranked ou loved". Un mirror qui
    annonce encore le statut graveyard pour une map entre-temps rankee ne doit
    pas la faire badger graveyard dans l'interface.
    """
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]

    def _ecrire(directory, rows):
        directory.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(rows, columns=COLUMNS)
        frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
        frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    _ecrire(tmp_path / "dump", features[:16])
    _ecrire(tmp_path / "grave", features[16:])

    # le manifeste pretend que TOUT est graveyard, y compris le dump
    annotations = {
        f["checksum"]: {"status": "graveyard", "playcount": 999}
        for f in features
    }
    report = build([tmp_path / "dump", tmp_path / "grave"], tmp_path / "index",
                   annotations=annotations)

    assert report["n_unranked"] == len(features) - 16, (
        "seules les maps venues du corpus non classe doivent etre marquees"
    )
    connection = sqlite3.connect(tmp_path / "index" / DB_FILE)
    try:
        connection.row_factory = sqlite3.Row
        par_statut = {r["status"]: r["n"] for r in connection.execute(
            "SELECT status, COUNT(*) n FROM maps GROUP BY status")}
    finally:
        connection.close()
    assert par_statut == {DUMP_STATUS: 16, "graveyard": len(features) - 16}


# --- ranked vs loved ----------------------------------------------------------

def test_sans_liste_loved_le_dump_reste_dump(tmp_path, mini_corpus):
    """
    Le dump melange ranked et loved sans les distinguer. Tant qu'on n'a pas la
    liste, ecrire `ranked` partout serait faux pour plusieurs milliers de maps :
    on dit `dump`, qui n'affirme rien.
    """
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]
    directory = tmp_path / "corpus"
    directory.mkdir()
    frame = pd.DataFrame(features, columns=COLUMNS)
    frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
    frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    report = build(directory, tmp_path / "index")
    assert report["n_loved"] == 0

    connection = sqlite3.connect(tmp_path / "index" / DB_FILE)
    try:
        statuts = {r[0] for r in connection.execute("SELECT DISTINCT status FROM maps")}
    finally:
        connection.close()
    assert statuts == {DUMP_STATUS}


def test_la_liste_loved_separe_ranked_et_loved(tmp_path, mini_corpus):
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]
    directory = tmp_path / "corpus"
    directory.mkdir()
    frame = pd.DataFrame(features, columns=COLUMNS)
    frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
    frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    loved = {f["checksum"] for f in features[:5]}
    report = build(directory, tmp_path / "index", loved=loved)

    assert report["n_loved"] == 5
    assert report["n_unranked"] == 0, "ni ranked ni loved ne sont 'non classes'"

    connection = sqlite3.connect(tmp_path / "index" / DB_FILE)
    try:
        connection.row_factory = sqlite3.Row
        par_statut = {r["status"]: r["n"] for r in connection.execute(
            "SELECT status, COUNT(*) n FROM maps GROUP BY status")}
    finally:
        connection.close()
    assert par_statut == {"ranked": len(features) - 5, "loved": 5}


def test_une_map_non_classee_ne_devient_pas_loved(tmp_path, mini_corpus):
    """
    Une map peut apparaitre dans les deux listes (statut perime chez le
    mirror). Le corpus dont elle vient tranche : venue du pool non classe, elle
    reste non classee, meme si son md5 traine dans la liste des loved.
    """
    pd = pytest.importorskip("pandas")
    from osumapmatcher.corpus import COLUMNS
    from osumapmatcher.features import FEATURE_NAMES
    from osumapmatcher.index import build

    features = [row[1] for row in mini_corpus]

    def _ecrire(directory, rows):
        directory.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(rows, columns=COLUMNS)
        frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
        frame.to_parquet(directory / "chunk_00000.parquet", index=False)

    _ecrire(tmp_path / "dump", features[:16])
    _ecrire(tmp_path / "grave", features[16:])

    annotations = {f["checksum"]: {"status": "graveyard", "playcount": 42}
                   for f in features[16:]}
    # la liste des loved pretend couvrir TOUT, y compris le pool non classe
    loved = {f["checksum"] for f in features}

    report = build([tmp_path / "dump", tmp_path / "grave"], tmp_path / "index",
                   annotations=annotations, loved=loved)

    assert report["n_unranked"] == len(features) - 16
    assert report["n_loved"] == 16, "seul le dump peut etre loved"


def test_le_fichier_loved_fait_un_aller_retour(tmp_path):
    from osumapmatcher.unranked import LOVED_FILE, load_loved, write_loved

    checksums = {"a" * 32, "b" * 32, "C" * 32}
    write_loved(tmp_path / LOVED_FILE, checksums)
    relu = load_loved(tmp_path)

    assert relu == {"a" * 32, "b" * 32, "c" * 32}, "les md5 sont normalises en minuscules"
    assert load_loved(tmp_path / "absent.json") == set()
