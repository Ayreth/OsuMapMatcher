#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
index.py - scaling, index ANN, et recherche des maps similaires.

Le scaling est le point critique. OsuMapMatcher faisait son KNN sur des features
brutes : une colonne en dizaines de milliers ecrasait mecaniquement une colonne
en 0-10, et la distance ne dependait plus que de deux ou trois dimensions.

Ici :
  * RobustScaler en quantile_range=(5, 95). Le (25, 75) par defaut laisse quatre
    colonnes du corpus avec un IQR nul -- sklearn remplace alors leur echelle
    par 1 et elles restent en unites brutes, ce qui reproduit exactement le bug
    d'origine. Mesure sur 2000 maps : (5, 95) ne laisse aucune colonne morte.
  * les valeurs scalees sont ecretees a +/- 5 : le corpus contient des maps
    aberrantes (jusqu'a 193 etoiles) qui, non bornees, se retrouvent a des
    centaines d'unites de tout le reste et degradent la geometrie de l'espace.
  * le scaler est persiste sous forme de tableaux (centre, echelle) plutot que
    picklable : rechargeable a l'identique, sans dependance a la version de
    sklearn qui l'a produit.

Recherche : pre-filtrage SQL (bande d'etoiles, longueur, meme beatmapset), puis
distance exacte ponderee par groupe sur les seuls candidats retenus. L'index ANN
prend le relais quand le pre-filtrage laisse trop de candidats.

Trois facons de poser la question, toutes servies par la meme `Searcher.search` :

  * une map           -- le cas d'origine ;
  * plusieurs maps    -- `combine_targets` en fait un centroide, et la bande
    d'etoiles couvre alors de la plus facile a la plus dure au lieu du centre,
    sans quoi deux graines eloignees s'excluraient mutuellement du resultat ;
  * une map et un contre-exemple -- `avoid`, qui penalise la proximite au
    repoussoir. Le choix de la formule est mesure et explique dans `search`.

Et une facon de trier ce qui en sort : `group_filter`, qui ne garde que les maps
dont l'ecart vient surtout d'un groupe donne. Il ne coute rien, `group_share`
etant deja calcule pour chaque resultat.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .features import (
    FEATURE_NAMES,
    GROUP_INDICES,
    GROUP_NAMES,
    VARIANTS,
    feature_vector,
    variant_for_mods,
)

__all__ = [
    "VARIANTS", "variant_for_mods", "Scaler", "Searcher", "SearcherSet",
    "Match", "QueryTarget", "SeedRef", "combine_targets",
    "build", "explain", "song_key", "normalise_weights",
    "DEFAULT_STAR_RANGE", "DEFAULT_LENGTH_RATIO", "DEFAULT_AVOID_STRENGTH",
    "DUMP_STATUS", "RANKED_STATUSES",
]

SCALER_FILE = "scaler.npz"
VECTORS_FILE = "vectors.npy"
RAW_FILE = "vectors_raw.npy"
INDEX_FILE = "index.usearch"
DB_FILE = "corpus.sqlite"

#: features expliquables en francais : (colonne, plus, moins, identique, format).
#: Sert a traduire une distance en phrase lisible ; l'ordre n'a pas d'importance,
#: le classement se fait sur l'ecart mesure.
EXPLAIN_SPEC: tuple[tuple[str, str, str, str, str], ...] = (
    ("bpm_median",     "faster",                 "slower",                 "same BPM",              "{:.0f} BPM"),
    ("object_density", "denser",                 "less dense",             "same density",          "{:.1f} obj/s"),
    ("dt_frac_lt100",  "more streams",           "fewer streams",          "as many streams",       "{:.0%}"),
    ("sp_p50",         "wider jumps",            "tighter jumps",          "same spacing",          "{:.1f}r"),
    ("vel_p50",        "faster cursor",          "slower cursor",          "same cursor speed",     "{:.0f}r/s"),
    ("aim_spike",      "spikier aim",            "steadier aim",           "same aim profile",      "x{:.1f}"),
    ("speed_spike",    "spikier tapping",        "steadier tapping",       "same tapping profile",  "x{:.1f}"),
    ("aim_speed_ratio", "more aim-heavy",        "more speed-heavy",       "same aim/speed balance", "{:.2f}"),
    ("aim_hot",        "difficulty more spread", "difficulty more concentrated", "same difficulty spread", "{:.0%}"),
    ("ratio_sliders",  "more sliders",           "fewer sliders",          "as many sliders",       "{:.0%}"),
    ("aimns_over_aim", "sliders matter less",    "sliders matter more",    "same slider weight",    "{:.2f}"),
    ("ang_150_180",    "more back-and-forth",    "less back-and-forth",    "same angles",           "{:.0%}"),
    ("cs",             "smaller circles",        "bigger circles",         "same CS",               "CS {:.1f}"),
    ("ar",             "higher AR",              "lower AR",               "same AR",               "AR {:.1f}"),
    ("drain_time_s",   "longer",                 "shorter",                "same length",           "mmss"),
)

#: ecart, en unites robustes, au-dela duquel une difference merite d'etre dite
EXPLAIN_DIFF_THRESHOLD = 0.30
#: et en-deca duquel deux maps se ressemblent vraiment sur cette dimension
EXPLAIN_SAME_THRESHOLD = 0.06
MAX_EXPLAIN_DIFFS = 3
MAX_EXPLAIN_SAMES = 2

#: au-dela de ce nombre de candidats on passe par l'index ANN au lieu de scorer
#: exactement tout le monde
EXACT_LIMIT = 30_000
#: facteur de sur-echantillonnage de l'index ANN, l'index ne sachant pas filtrer
OVERSAMPLE = 30

#: statut des maps venues du dump ppy. Volontairement `dump` et pas `ranked` :
#: le dump melange ranked et loved sans les distinguer, et pretendre le
#: contraire dans une colonne serait un petit mensonge que l'interface
#: repeterait. Tout ce qui n'est pas `dump` vient de `fetch_unranked.py`.
DUMP_STATUS = "dump"

#: statuts qu'on ne considere pas comme "non classe", donc telechargeables
#: normalement depuis le jeu
RANKED_STATUSES = frozenset({DUMP_STATUS, "ranked", "approved", "loved"})

#: annotation par defaut : ce dont on ne sait rien vient du dump
_NO_MARK: dict[str, Any] = {"status": DUMP_STATUS, "playcount": None}
#: et quand on dispose de la liste des loved, on sait trancher
_RANKED_MARK: dict[str, Any] = {"status": "ranked", "playcount": None}
_LOVED_MARK: dict[str, Any] = {"status": "loved", "playcount": None}


DEFAULT_STAR_RANGE = 0.5
DEFAULT_LENGTH_RATIO = (0.5, 2.0)
#: constante de temps de la conversion distance -> pourcentage de similarite
SIMILARITY_SCALE = 6.0

#: force de la penalite du contre-exemple, mesuree (voir `search`). A 0,15 elle
#: ne mord pas assez pour changer quoi que ce soit sur certaines cibles ; a 0,30
#: elle coute 3 % de distance a la cible et rend 25 % de distance au repoussoir.
DEFAULT_AVOID_STRENGTH = 0.30
#: et au-dela de ca, "comme celle-ci" ne veut plus rien dire : mesure sur une map
#: a 5,5 etoiles, un beta de 0,90 sort un premier resultat a 3,34 de la cible la
#: ou la recherche ordinaire trouve 1,11. La raison est geometrique, voir `search`.
MAX_AVOID_STRENGTH = 0.75



# --- scaler -------------------------------------------------------------------

@dataclass(frozen=True)
class Scaler:
    center: np.ndarray
    scale: np.ndarray
    clip: float
    feature_names: tuple[str, ...]

    @classmethod
    def fit(
        cls,
        matrix: np.ndarray,
        *,
        clip: float = 5.0,
        quantile_range: tuple[float, float] = (5.0, 95.0),
    ) -> "Scaler":
        from sklearn.preprocessing import RobustScaler

        fitted = RobustScaler(quantile_range=quantile_range).fit(matrix)
        scale = np.asarray(fitted.scale_, dtype=np.float64).copy()
        scale[scale == 0] = 1.0  # ceinture et bretelles, sklearn le fait deja
        return cls(
            center=np.asarray(fitted.center_, dtype=np.float64).copy(),
            scale=scale,
            clip=float(clip),
            feature_names=tuple(FEATURE_NAMES),
        )

    def transform(self, matrix: np.ndarray) -> np.ndarray:
        values = np.atleast_2d(np.asarray(matrix, dtype=np.float64))
        out = (values - self.center) / self.scale
        np.clip(out, -self.clip, self.clip, out=out)
        # ordre C impose : `DataFrame.to_numpy()` rend un tableau colonne-major,
        # et une ligne d'un tableau colonne-major n'est pas contigue -- usearch
        # la refuse, et la lecture d'un bloc de lignes va chercher ses octets
        # aux quatre coins du fichier.
        return np.ascontiguousarray(out, dtype=np.float32)

    def save(self, path: Path) -> None:
        np.savez(
            path,
            center=self.center,
            scale=self.scale,
            clip=np.array([self.clip]),
            feature_names=np.array(self.feature_names, dtype=object),
        )

    @classmethod
    def load(cls, path: Path) -> "Scaler":
        with np.load(path, allow_pickle=True) as data:
            names = tuple(str(n) for n in data["feature_names"])
            if names != tuple(FEATURE_NAMES):
                raise ValueError(
                    "le scaler sur disque ne correspond plus a FEATURE_NAMES : "
                    "reconstruire l'index (scripts/build_index.py)"
                )
            return cls(
                center=data["center"],
                scale=data["scale"],
                clip=float(data["clip"][0]),
                feature_names=names,
            )


# --- resultat -----------------------------------------------------------------

def _row_get(row: sqlite3.Row, key: str, default: Any) -> Any:
    """
    Lit une colonne qui peut ne pas exister.

    Le launcher installe un index prebati telecharge depuis une release : un
    utilisateur peut tourner avec un code a jour sur un index d'avant l'ajout
    des colonnes `status` et `playcount`. Planter la-dessus rendrait la mise a
    jour du code obligatoirement solidaire d'un retelechargement de 165 Mo.
    """
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


@dataclass
class Match:
    rank: int
    checksum: str
    beatmap_id: int
    beatmapset_id: int
    artist: str
    title: str
    creator: str
    version: str
    stars: float
    bpm: float
    length_s: float
    distance: float
    group_distance: dict[str, float] = field(default_factory=dict)
    group_share: dict[str, float] = field(default_factory=dict)
    differences: list[str] = field(default_factory=list)
    similarities: list[str] = field(default_factory=list)
    #: `dump` (ranked ou loved, le dump ne les separe pas) ou le statut reel
    #: d'une map recuperee en ligne : graveyard, pending, wip...
    status: str = "dump"
    #: nombre de plays, connu seulement pour les maps non classees -- c'est le
    #: critere qui les a fait entrer dans le pool, donc il merite d'etre montre
    playcount: int | None = None

    @property
    def similarity(self) -> float:
        """
        Distance rendue lisible, entre 0 et 1.

        Une distance euclidienne n'a pas de borne haute et ne parle a personne ;
        `exp(-d / 6)` est strictement decroissante, donc elle ne change aucun
        classement, et cale les valeurs sur une echelle ou un tres bon voisin
        tourne autour de 80 % et un resultat lointain autour de 30 %. La
        distance brute reste affichee dans le detail.
        """
        return float(math.exp(-self.distance / SIMILARITY_SCALE))

    @property
    def url_web(self) -> str:
        return f"https://osu.ppy.sh/b/{self.beatmap_id}"

    @property
    def url_cover(self) -> str:
        """
        Vignette du beatmapset. Seule ressource distante du projet : la page la
        charge directement, et s'en passe si elle n'arrive pas. Rien ici ne
        depend du reseau, ni la recherche ni les features.
        """
        if not self.beatmapset_id:
            return ""
        return f"https://assets.ppy.sh/beatmaps/{self.beatmapset_id}/covers/list@2x.jpg"

    @property
    def url_direct(self) -> str:
        return f"osu://b/{self.beatmap_id}"

    @property
    def is_unranked(self) -> bool:
        """
        Cette map sort-elle du pool non classe ?

        L'interface en a besoin pour la signaler : on ne clique pas sur
        osu!direct de la meme facon selon qu'on tombe sur une map ranked ou sur
        du graveyard, et certains voudront les filtrer.
        """
        return self.status not in RANKED_STATUSES

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}]"

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "rank": self.rank,
            "checksum": self.checksum,
            "beatmap_id": self.beatmap_id,
            "beatmapset_id": self.beatmapset_id,
            "artist": self.artist,
            "title": self.title,
            "creator": self.creator,
            "version": self.version,
            "stars": round(self.stars, 2),
            "bpm": round(self.bpm, 1),
            "length_s": round(self.length_s, 1),
            "distance": round(self.distance, 4),
            "similarity": round(self.similarity, 4),
            "group_distance": {k: round(v, 4) for k, v in self.group_distance.items()},
            "group_share": {k: round(v, 4) for k, v in self.group_share.items()},
            "differences": self.differences,
            "similarities": self.similarities,
            "status": self.status,
            "is_unranked": self.is_unranked,
            "playcount": self.playcount,
            "url_web": self.url_web,
            "url_direct": self.url_direct,
            "url_cover": self.url_cover,
        }
        return payload


# --- construction -------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    pos            INTEGER PRIMARY KEY,
    checksum       TEXT    NOT NULL UNIQUE,
    beatmap_id     INTEGER NOT NULL,
    beatmapset_id  INTEGER NOT NULL,
    artist         TEXT,
    title          TEXT,
    creator        TEXT,
    version        TEXT,
    stars          REAL,
    bpm            REAL,
    length_s       REAL,
    n_objects      INTEGER,
    status         TEXT    NOT NULL DEFAULT 'dump',
    playcount      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_maps_stars ON maps(stars);
CREATE INDEX IF NOT EXISTS idx_maps_set   ON maps(beatmapset_id);
CREATE INDEX IF NOT EXISTS idx_maps_bid   ON maps(beatmap_id);
"""


def build(
    corpus_dir: Path | Sequence[Path],
    data_dir: Path,
    *,
    clip: float = 5.0,
    annotations: dict[str, dict[str, Any]] | None = None,
    loved: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """
    Fabrique scaler + vecteurs + index ANN + SQLite depuis les parquets.

    `corpus_dir` accepte plusieurs dossiers : le dump ppy d'un cote, les maps
    non classees recuperees en ligne de l'autre. Ils restent separes sur disque
    parce qu'ils se rafraichissent a des rythmes differents, et que la reprise
    par tranches de `build_corpus` est indexee par position -- les melanger
    invaliderait la reprise a chaque ajout.

    `annotations` (issu du manifeste de `fetch_unranked.py`) fournit le statut
    et le playcount, qui ne sont nulle part dans un .osu. La jointure se fait
    sur le checksum, seule cle fiable : certains .osu du graveyard portent un
    `BeatmapID:` qui n'est pas le leur. Ce qui n'y figure pas est du dump.

    `loved` est l'ensemble des md5 loved. Fourni, il permet d'etiqueter le
    dump en `ranked` / `loved` ; absent, tout le dump reste `dump`.
    """
    import pandas as pd
    from usearch.index import Index

    from .corpus import load_corpus

    data_dir.mkdir(parents=True, exist_ok=True)
    directories = (
        [Path(corpus_dir)]
        if isinstance(corpus_dir, (str, Path))
        else [Path(d) for d in corpus_dir]
    )
    frames = [load_corpus(directory) for directory in directories]
    for rang, part in enumerate(frames):
        part["_origine"] = rang
    frame = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
    # `keep="first"` avec le dump en tete : une map qui serait passee ranked
    # entre le dump et la recuperation garde sa version officielle
    frame = frame.drop_duplicates(subset="checksum", keep="first").reset_index(drop=True)

    annotations = annotations or {}
    loved = loved or frozenset()
    # Les annotations ne s'appliquent qu'aux corpus qui viennent APRES le
    # premier. Le dump est autoritaire sur "cette map est ranked ou loved" :
    # sans ce garde-fou, une map du dump dont le mirror annonce encore le
    # statut graveyard se retrouvait badgee graveyard dans l'interface. Mesure
    # sur le lot reel : 4 maps concernees.
    #
    # Pour le dump lui-meme, deux cas. Avec une liste de loved, on sait dire
    # `ranked` ou `loved`. Sans elle, on dit `dump` -- qui ne ment sur rien : le
    # dump melange les deux sans les distinguer, et ecrire `ranked` partout
    # serait une affirmation qu'on ne peut pas soutenir.
    defaut_dump = _NO_MARK if not loved else None
    marks = []
    for checksum, origine in zip(frame["checksum"], frame["_origine"]):
        if origine > 0:
            marks.append(annotations.get(str(checksum), _NO_MARK))
        elif defaut_dump is not None:
            marks.append(defaut_dump)
        else:
            marks.append(_LOVED_MARK if str(checksum) in loved else _RANKED_MARK)

    statuses = [mark["status"] for mark in marks]
    playcounts = [mark["playcount"] for mark in marks]
    n_unranked = sum(1 for s in statuses if s not in RANKED_STATUSES)
    n_loved = sum(1 for s in statuses if s == "loved")
    frame = frame.drop(columns="_origine")

    raw = frame[list(FEATURE_NAMES)].to_numpy(dtype=np.float64)
    scaler = Scaler.fit(raw, clip=clip)
    scaler.save(data_dir / SCALER_FILE)

    vectors = scaler.transform(raw)
    np.save(data_dir / VECTORS_FILE, vectors)
    # valeurs brutes conservees a part : les vecteurs scales sont ecretes, donc
    # inversibles seulement approximativement, et les explications affichent des
    # vrais BPM et de vraies densites
    np.save(data_dir / RAW_FILE, raw.astype(np.float32))

    # SQLite : la cle primaire `pos` est la position dans le tableau de vecteurs
    db_path = data_dir / DB_FILE
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        rows = zip(
            range(len(frame)),
            frame["checksum"],
            frame["beatmap_id"].astype("int64"),
            frame["beatmapset_id"].astype("int64"),
            frame["artist"], frame["title"], frame["creator"], frame["version"],
            frame["stars"].astype("float64"),
            frame["bpm_median"].astype("float64"),
            frame["drain_time_s"].astype("float64"),
            frame["n_objects"].astype("int64"),
            statuses,
            playcounts,
        )
        connection.executemany(
            "INSERT INTO maps (pos, checksum, beatmap_id, beatmapset_id, artist,"
            " title, creator, version, stars, bpm, length_s, n_objects,"
            " status, playcount)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()

    index = Index(ndim=vectors.shape[1], metric="l2sq", dtype="f32")
    # usearch exige des lignes contigues en memoire
    index.add(
        np.arange(len(vectors), dtype=np.int64),
        np.ascontiguousarray(vectors, dtype=np.float32),
        threads=0,
    )
    index.save(data_dir / INDEX_FILE)

    report = {
        "n_maps": int(len(frame)),
        "n_unranked": int(n_unranked),
        "n_loved": int(n_loved),
        "n_sources": len(directories),
        "n_features": len(FEATURE_NAMES),
        "clip": clip,
        "vectors_mb": round((data_dir / VECTORS_FILE).stat().st_size / 1e6, 1),
        "index_mb": round((data_dir / INDEX_FILE).stat().st_size / 1e6, 1),
        "sqlite_mb": round(db_path.stat().st_size / 1e6, 1),
    }
    (data_dir / "index_manifest.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


# --- recherche ----------------------------------------------------------------

_FEATURE_POSITION = {name: i for i, name in enumerate(FEATURE_NAMES)}
_EXPLAIN_RESOLVED = tuple(
    (_FEATURE_POSITION[name], more, less, same, fmt)
    for name, more, less, same, fmt in EXPLAIN_SPEC
    if name in _FEATURE_POSITION
)


def _format(spec: str, value: float) -> str:
    if spec == "mmss":
        return f"{int(value) // 60}:{int(value) % 60:02d}"
    try:
        return spec.format(value)
    except (ValueError, TypeError):
        return f"{value:.2f}"


def explain(
    query_scaled: np.ndarray, query_raw: np.ndarray,
    match_scaled: np.ndarray, match_raw: np.ndarray,
) -> tuple[list[str], list[str]]:
    """
    Traduit une distance en phrases.

    L'ampleur d'un ecart se juge en unites robustes (comparables entre
    colonnes), mais les valeurs affichees sont les vraies : dire "plus dense
    (2.4 -> 3.9 obj/s)" est utile, "plus dense (+0.8 unites)" ne l'est pas.
    """
    scored = []
    for position, more, less, same, fmt in _EXPLAIN_RESOLVED:
        gap = float(match_scaled[position]) - float(query_scaled[position])
        scored.append((abs(gap), gap, position, more, less, same, fmt))
    scored.sort(reverse=True, key=lambda row: row[0])

    differences: list[str] = []
    for magnitude, gap, position, more, less, _same, fmt in scored:
        if magnitude < EXPLAIN_DIFF_THRESHOLD or len(differences) >= MAX_EXPLAIN_DIFFS:
            continue
        label = more if gap > 0 else less
        differences.append(
            f"{label} ({_format(fmt, float(query_raw[position]))}"
            f" -> {_format(fmt, float(match_raw[position]))})"
        )

    similarities: list[str] = []
    for magnitude, _gap, _position, _more, _less, same, _fmt in reversed(scored):
        if magnitude > EXPLAIN_SAME_THRESHOLD or len(similarities) >= MAX_EXPLAIN_SAMES:
            continue
        similarities.append(same)
    return differences, similarities


def song_key(artist: str, title: str) -> str:
    """
    Cle de dedoublonnage. Volontairement fondee sur le seul titre : le champ
    artiste est trop instable pour regrouper une meme chanson ("Aiurabu" contre
    "Aiurabu (CV: Nakajima Yui, ...)"), et deux reprises differentes du meme
    morceau sont bien la meme chanson du point de vue de l'ecoute.
    """
    return " ".join((title or "").split()).lower()


def normalise_weights(weights: dict[str, float] | None) -> dict[str, float]:
    resolved = {group: 1.0 for group in GROUP_NAMES}
    for group, value in (weights or {}).items():
        if group not in resolved:
            raise KeyError(f"Unknown weight group: {group}")
        resolved[group] = max(0.0, float(value))
    if all(value == 0.0 for value in resolved.values()):
        raise ValueError("Every weight is zero, no distance can be computed.")
    return resolved


def _weight_vector(weights: dict[str, float]) -> np.ndarray:
    out = np.ones(len(FEATURE_NAMES), dtype=np.float32)
    for group, value in weights.items():
        out[GROUP_INDICES[group]] = value
    return out


@dataclass(frozen=True)
class SeedRef:
    """
    Une graine d'une recherche multi-maps, une fois le centroide calcule.

    Son vecteur n'est plus la -- il a ete moyenne -- mais ses etoiles, sa duree
    et son beatmapset servent encore au pre-filtrage, et son libelle a
    l'interface, qui doit pouvoir montrer de quoi la moyenne est faite.
    """

    checksum: str
    beatmap_id: int
    beatmapset_id: int
    stars: float
    length_s: float
    bpm: float
    label: str

    @classmethod
    def of(cls, target: "QueryTarget") -> "SeedRef":
        return cls(
            checksum=target.checksum,
            beatmap_id=target.beatmap_id,
            beatmapset_id=target.beatmapset_id,
            stars=target.stars,
            length_s=target.length_s,
            bpm=target.bpm,
            label=target.label,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checksum": self.checksum,
            "beatmap_id": self.beatmap_id,
            "beatmapset_id": self.beatmapset_id,
            "stars": round(self.stars, 2),
            "bpm": round(self.bpm, 1),
            "length_s": round(self.length_s, 1),
            "label": self.label,
        }


@dataclass
class QueryTarget:
    vector: np.ndarray
    raw: np.ndarray
    stars: float
    length_s: float
    bpm: float
    beatmapset_id: int
    checksum: str
    label: str
    beatmap_id: int = 0
    #: graines d'une recherche multi-maps. Vide pour une cible ordinaire, et
    #: tout ce qui suit se comporte alors exactement comme avant.
    seeds: tuple[SeedRef, ...] = ()

    @property
    def is_centroid(self) -> bool:
        """
        Cette cible est-elle une map fictive ?

        Ca change ce que veulent dire les explications : elles ne comparent plus
        a une vraie map mais a la moyenne des choix de l'utilisateur, et
        l'interface doit le dire au lieu de laisser croire le contraire.
        """
        return len(self.seeds) > 1

    @property
    def star_span(self) -> tuple[float, float]:
        """
        Bande d'etoiles a couvrir AVANT d'y ajouter `star_range`.

        Avec des graines a 4 et 7 etoiles, le centroide tombe a 5,5 : garder
        `centre +/- range` ecarterait mecaniquement les deux extremites, donc
        exactement les maps qui ressemblent le plus a ce qu'on a demande. On
        couvre donc [min, max], que `star_range` elargit ensuite des deux cotes.
        """
        if not self.seeds:
            return (self.stars, self.stars)
        values = [seed.stars for seed in self.seeds]
        return (min(values), max(values))

    @property
    def length_span(self) -> tuple[float, float]:
        """Meme raisonnement que `star_span`, pour la duree."""
        if not self.seeds:
            return (self.length_s, self.length_s)
        values = [seed.length_s for seed in self.seeds]
        return (min(values), max(values))

    @property
    def excluded_sets(self) -> tuple[int, ...]:
        """Beatmapsets a ecarter : celui de CHAQUE graine, pas seulement un."""
        found = {seed.beatmapset_id for seed in self.seeds if seed.beatmapset_id}
        if self.beatmapset_id:
            found.add(self.beatmapset_id)
        return tuple(sorted(found))

    @property
    def excluded_checksums(self) -> tuple[str, ...]:
        """Une graine ne peut pas etre sa propre recommandation."""
        found = {seed.checksum for seed in self.seeds if seed.checksum}
        if self.checksum:
            found.add(self.checksum)
        return tuple(sorted(found))


def combine_targets(targets: Sequence[QueryTarget]) -> QueryTarget:
    """
    Reduit plusieurs graines a une cible unique : le centroide de leurs vecteurs.

    La moyenne se fait dans l'espace SCALE, celui ou vivent les distances -- une
    moyenne de valeurs brutes serait dominee par les colonnes en grandes unites,
    ce qui est le bug d'origine du projet. Les valeurs brutes sont moyennees
    aussi, mais uniquement pour les explications : "180 BPM" y designe alors la
    moyenne des BPM choisis, pas ceux d'une map reelle.

    Pas de ponderation des graines : mesure faite, une ponderation par nombre de
    plays ou par recence deplacait le centroide de moins que le bruit du corpus
    sur les jeux d'essai. Une option qui ne change rien est une option de trop.

    Une graine unique est renvoyee telle quelle, avec sa propre identite : il n'y
    a aucune raison de fabriquer une map fictive la ou il y a une vraie map, et
    les explications resteraient fausses pour rien.
    """
    if not targets:
        raise ValueError("no seed to search from")

    seeds = tuple(SeedRef.of(target) for target in targets)
    if len(targets) == 1:
        only = targets[0]
        return QueryTarget(
            vector=only.vector, raw=only.raw,
            stars=only.stars, length_s=only.length_s, bpm=only.bpm,
            beatmapset_id=only.beatmapset_id, checksum=only.checksum,
            label=only.label, beatmap_id=only.beatmap_id, seeds=seeds,
        )

    vectors = np.stack([np.asarray(t.vector, dtype=np.float32) for t in targets])
    raws = np.stack([np.asarray(t.raw, dtype=np.float32) for t in targets])
    count = len(targets)
    return QueryTarget(
        # contigu : la cible part telle quelle dans usearch sur le chemin ANN
        vector=np.ascontiguousarray(vectors.mean(axis=0), dtype=np.float32),
        raw=np.ascontiguousarray(raws.mean(axis=0), dtype=np.float32),
        stars=float(np.mean([t.stars for t in targets])),
        length_s=float(np.mean([t.length_s for t in targets])),
        bpm=float(np.mean([t.bpm for t in targets])),
        # un centroide n'est aucune map : lui preter un beatmapset ou un
        # checksum ferait mentir tout ce qui s'en sert (exclusions, liens)
        beatmapset_id=0,
        checksum="",
        label=f"centroid of {count} maps",
        beatmap_id=0,
        seeds=seeds,
    )


class Searcher:
    """Charge le scaler, les vecteurs (memory-mapped), l'index et le SQLite."""

    def __init__(self, data_dir: Path) -> None:
        from usearch.index import Index

        self.data_dir = Path(data_dir)
        # Une connexion sqlite ouverte en check_same_thread=False n'est pas
        # reentrante : deux threads qui l'utilisent en meme temps se volent le
        # curseur, et `row["pos"]` finit par lever IndexError sur une ligne dont
        # la description ne correspond plus. C'est arrive en vrai des que deux
        # recherches se chevauchaient. Le verrou vit ici, dans l'objet qui
        # possede la connexion, plutot que chez chaque appelant -- il y en a
        # trois, et il en suffit d'un qui oublie.
        self._db_lock = threading.RLock()
        self.scaler = Scaler.load(self.data_dir / SCALER_FILE)
        # mmap : les pages ne sont lues que quand on touche vraiment les lignes
        self.vectors = np.load(self.data_dir / VECTORS_FILE, mmap_mode="r")
        raw_path = self.data_dir / RAW_FILE
        if not raw_path.exists():
            raise FileNotFoundError(
                f"{raw_path} absent : index construit par une version anterieure, "
                "relance scripts/build_index.py"
            )
        self.raw_vectors = np.load(raw_path, mmap_mode="r")
        self.db = sqlite3.connect(
            f"file:{(self.data_dir / DB_FILE).as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        self.index = Index.restore(str(self.data_dir / INDEX_FILE), view=True)
        # les index d'avant l'ajout des maps non classees n'ont pas la colonne.
        # Le launcher sert un index prebati depuis une release : quelqu'un peut
        # tres bien tourner sur un ancien zip avec un code a jour.
        self.has_status = self._has_column("status")
        self._filters = self._load_filters()

    # -- colonnes de filtrage --------------------------------------------------

    def _has_column(self, name: str) -> bool:
        with self._db_lock:
            rows = self.db.execute("PRAGMA table_info(maps)").fetchall()
        return any(row["name"] == name for row in rows)

    def _load_filters(
        self,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Monte en memoire les colonnes qui servent au pre-filtrage.

        Le meme filtre en SQL coute 250 ms sur une bande d'etoiles dense : un
        index sur `stars` trouve bien les lignes, mais tester la longueur et le
        beatmapset oblige sqlite a aller lire 30 000 lignes une par une. Les
        memes conditions en numpy sur trois tableaux de 151 k valeurs prennent
        moins d'une milliseconde, pour 3,6 Mo de memoire et une lecture au
        chargement -- et sans toucher aux index deja distribues.

        Le statut suit la meme logique : un booleen par map, 151 ko, teste en
        meme temps que le reste plutot qu'en SQL.
        """
        columns = "pos, stars, length_s, beatmapset_id"
        if self.has_status:
            columns += ", status"
        with self._db_lock:
            rows = self.db.execute(f"SELECT {columns} FROM maps").fetchall()
        size = max((row["pos"] for row in rows), default=-1) + 1
        stars = np.zeros(size, dtype=np.float32)
        lengths = np.zeros(size, dtype=np.float32)
        sets = np.zeros(size, dtype=np.int64)
        # `pos` est dense en pratique, mais un trou ne doit pas fabriquer un
        # candidat fantome a 0 etoile
        valid = np.zeros(size, dtype=bool)
        unranked = np.zeros(size, dtype=bool)
        for row in rows:
            position = row["pos"]
            stars[position] = row["stars"]
            lengths[position] = row["length_s"]
            sets[position] = row["beatmapset_id"]
            valid[position] = True
            if self.has_status:
                unranked[position] = row["status"] not in RANKED_STATUSES
        return stars, lengths, sets, valid, unranked

    def _position_of(self, checksum: str) -> int:
        with self._db_lock:
            row = self.db.execute(
                "SELECT pos FROM maps WHERE checksum = ?", (checksum,)
            ).fetchone()
        return int(row["pos"]) if row else -1

    # -- resolution de la requete ---------------------------------------------

    def target_from_checksum(self, checksum: str) -> QueryTarget | None:
        with self._db_lock:
            row = self.db.execute(
                "SELECT pos, stars, length_s, bpm, beatmap_id, beatmapset_id,"
                " artist, title, version FROM maps WHERE checksum = ?",
                (checksum,),
            ).fetchone()
        if row is None:
            return None
        return QueryTarget(
            # copie contigue : les vecteurs sont mappes en memoire et les index
            # bâtis avant ce correctif sont colonne-major, donc une ligne prise
            # telle quelle n'est pas contigue (usearch la refuse)
            vector=np.ascontiguousarray(self.vectors[row["pos"]], dtype=np.float32),
            raw=np.ascontiguousarray(self.raw_vectors[row["pos"]], dtype=np.float32),
            stars=float(row["stars"]),
            length_s=float(row["length_s"]),
            bpm=float(row["bpm"]),
            beatmapset_id=int(row["beatmapset_id"]),
            checksum=checksum,
            label=f'{row["artist"]} - {row["title"]} [{row["version"]}]',
            beatmap_id=int(row["beatmap_id"]),
        )

    def has_checksum(self, checksum: str) -> bool:
        """Cette map est-elle dans le corpus ? Passe par le verrou, comme tout
        le reste : le SQL ne doit pas fuir hors de cette classe."""
        return bool(checksum) and self._position_of(checksum) >= 0

    def target_from_beatmap_id(self, beatmap_id: int) -> QueryTarget | None:
        with self._db_lock:
            row = self.db.execute(
                "SELECT checksum FROM maps WHERE beatmap_id = ? LIMIT 1", (beatmap_id,)
            ).fetchone()
        return self.target_from_checksum(row["checksum"]) if row else None

    def target_from_features(self, features: dict[str, Any]) -> QueryTarget:
        """Chemin live : une map qui n'est pas forcement dans le corpus."""
        raw = feature_vector(features)
        vector = self.scaler.transform(raw)[0]
        return QueryTarget(
            vector=vector,
            raw=raw,
            stars=float(features.get("stars", 0.0)),
            length_s=float(features["drain_time_s"]),
            bpm=float(features["bpm_median"]),
            beatmapset_id=int(features.get("beatmapset_id", 0)),
            checksum=str(features.get("checksum", "")),
            label=f'{features.get("artist", "")} - {features.get("title", "")}'
                  f' [{features.get("version", "")}]',
            beatmap_id=int(features.get("beatmap_id", 0)),
        )

    # -- pre-filtrage ----------------------------------------------------------

    def _candidates(
        self,
        target: QueryTarget,
        *,
        star_range: float,
        length_ratio: tuple[float, float] | None,
        exclude_same_set: bool,
        allow_unranked: bool = True,
        avoid: QueryTarget | None = None,
    ) -> np.ndarray:
        stars, lengths, sets, valid, unranked = self._filters
        # `star_span` vaut (stars, stars) pour une cible ordinaire : la bande
        # reste alors identique a ce qu'elle a toujours ete. Avec plusieurs
        # graines elle couvre de la plus facile a la plus dure, voir QueryTarget.
        star_low, star_high = target.star_span
        keep = valid & (stars >= star_low - star_range) & (stars <= star_high + star_range)
        if not allow_unranked:
            keep &= ~unranked
        if length_ratio:
            length_low, length_high = target.length_span
            if length_low > 0:
                keep &= lengths >= length_low * length_ratio[0]
            if length_high > 0:
                keep &= lengths <= length_high * length_ratio[1]
        excluded_sets = set(target.excluded_sets)
        excluded_checksums = set(target.excluded_checksums)
        if avoid is not None:
            # repondre "pas comme celle-la" en proposant celle-la serait absurde,
            # et la penalite ne suffit pas a l'ecarter : le repoussoir est a
            # distance nulle de lui-meme, donc c'est lui que la penalite epargne
            # le plus. Mesure a beta=0,15 : il ressortait 4e.
            excluded_checksums |= set(avoid.excluded_checksums)
            if exclude_same_set:
                excluded_sets |= set(avoid.excluded_sets)
        if exclude_same_set and excluded_sets:
            if len(excluded_sets) == 1:
                keep &= sets != next(iter(excluded_sets))
            else:
                keep &= ~np.isin(sets, np.asarray(sorted(excluded_sets), dtype=np.int64))
        for checksum in sorted(excluded_checksums):
            # une graine ne peut pas etre sa propre recommandation ; une map
            # live absente du corpus n'a simplement pas de position
            position = self._position_of(checksum)
            if position >= 0:
                keep[position] = False
        return np.flatnonzero(keep).astype(np.int64)

    # -- recherche -------------------------------------------------------------

    def search(
        self,
        target: QueryTarget,
        *,
        k: int = 20,
        star_range: float = DEFAULT_STAR_RANGE,
        length_ratio: tuple[float, float] | None = DEFAULT_LENGTH_RATIO,
        exclude_same_set: bool = True,
        weights: dict[str, float] | None = None,
        max_per_song: int = 1,
        diverge: str | None = None,
        allow_unranked: bool = True,
        group_filter: str | None = None,
        avoid: QueryTarget | None = None,
        avoid_strength: float = DEFAULT_AVOID_STRENGTH,
    ) -> list[Match]:
        """
        Les voisins de `target`, filtres puis classes.

        `avoid` est le contre-exemple : "comme celle-ci, mais pas comme
        celle-la". Le classement se fait alors sur

            score = d(x, cible)^2 - beta * d(x, repoussoir)^2

        Deux pistes avaient ete envisagees, penalite et marge dure (ne garder que
        les maps plus proches de la cible que du repoussoir). La marge dure a ete
        ecartee sur mesure : sa severite depend entierement de la distance entre
        cible et repoussoir, que l'utilisateur ne controle pas. Sur deux cas
        reels elle gardait 33 % des candidats dans l'un et 6 % dans l'autre --
        soit presque rien, soit presque tout, sans que rien ne l'annonce.

        La penalite, elle, est previsible, et pour une bonne raison : a beta fixe
        elle equivaut EXACTEMENT a chercher autour du point extrapole
        `(c - beta*r) / (1 - beta)`, verifie numeriquement sur les 50 premiers
        resultats. C'est aussi ce qui impose `MAX_AVOID_STRENGTH` : quand beta
        tend vers 1 ce point part a l'infini, hors des donnees, et la recherche
        cesse de vouloir dire "comme celle-ci".

        Ce qu'elle coute et ce qu'elle rend, mesure sur une map a 5,5 etoiles
        dont le repoussoir est le voisin #1 :

            beta    d(cible)   d(repoussoir)   communs avec la liste de base
            0.00      1.114        1.121                10/10
            0.15      1.122        1.235                 7/10
            0.30      1.146        1.396                 6/10
            0.50      1.188        1.471                 5/10
            0.90      2.585        2.966                 0/10

        `Match.distance` reste la vraie distance a la cible : le score penalise
        classe, il ne s'affiche pas. Un pourcentage de similarite qui bougerait
        parce qu'on a nomme un repoussoir mentirait sur ce qu'il mesure.
        """
        if diverge and diverge not in GROUP_NAMES:
            raise KeyError(f"Unknown diverge group: {diverge}")
        if group_filter and group_filter not in GROUP_NAMES:
            raise KeyError(f"Unknown group filter: {group_filter}")
        if not 0.0 <= avoid_strength <= MAX_AVOID_STRENGTH:
            raise ValueError(
                f"avoid_strength must be between 0 and {MAX_AVOID_STRENGTH}."
            )
        resolved = normalise_weights(weights)
        weight_vector = _weight_vector(resolved)

        candidates = self._candidates(
            target,
            star_range=star_range,
            length_ratio=length_ratio,
            exclude_same_set=exclude_same_set,
            allow_unranked=allow_unranked,
            avoid=avoid,
        )
        if candidates.size == 0:
            return []

        # Le point autour duquel l'ANN doit degrossir. Sans repoussoir c'est la
        # cible ; avec, c'est le point extrapole -- le score penalise classe
        # exactement comme la distance a ce point-la, donc degrossir autour de
        # lui est le bon pre-tri et pas une approximation de plus.
        probe = target.vector
        if avoid is not None and avoid_strength > 0.0:
            probe = (target.vector - avoid_strength * avoid.vector) / (
                1.0 - avoid_strength
            )

        # L'index ANN classe par distance totale ; en mode divergence on cherche
        # au contraire les plus eloignes sur un groupe, donc son pre-tri
        # biaiserait le resultat. On score alors tout le monde exactement.
        if candidates.size > EXACT_LIMIT and not diverge:
            # Trop de candidats pour un scoring exact : l'index ANN degrossit,
            # puis on re-score exactement les survivants du filtre.
            # un groupe dominant sur cinq : sans elargir ici, le filtre couperait
            # dans une liste deja tronquee par l'ANN et rendrait moins de k
            wanted = min(k * OVERSAMPLE * (5 if group_filter else 1), int(self.index.size))
            # ceinture et bretelles : la cible peut venir d'un appelant qui a
            # fabrique son vecteur autrement (l'API distante, par exemple)
            hits = self.index.search(
                np.ascontiguousarray(probe, dtype=np.float32), count=wanted
            )
            keys = np.asarray(hits.keys, dtype=np.int64).ravel()
            allowed = np.isin(keys, candidates, assume_unique=False)
            narrowed = keys[allowed]
            # si l'ANN ne ramene pas assez de candidats valides, on retombe sur
            # le scoring exact plutot que de rendre une liste tronquee
            candidates = narrowed if narrowed.size >= k else candidates

        block = np.asarray(self.vectors[candidates], dtype=np.float32)
        delta = (block - target.vector) * weight_vector
        squared = delta * delta
        total = squared.sum(axis=1)
        per_group_all = {
            group: squared[:, GROUP_INDICES[group]].sum(axis=1) for group in GROUP_NAMES
        }

        # `score` est ce qui CLASSE, `total` ce qui s'affiche. Sans repoussoir
        # les deux sont le meme tableau et rien ne change.
        score = total
        if avoid is not None and avoid_strength > 0.0:
            gap = (block - avoid.vector) * weight_vector
            score = total - avoid_strength * (gap * gap).sum(axis=1)

        if diverge:
            # "pareil mais different" : on reste proche sur tous les groupes sauf
            # un, puis on classe par eloignement maximal sur celui-la. C'est ce
            # qu'on veut pour s'entrainer -- meme competence, autre habillage.
            near = score - per_group_all[diverge]
            pool_size = min(near.size, max(k * 20, 200))
            pool = np.argpartition(near, pool_size - 1)[:pool_size]
            ordering = pool[np.argsort(-per_group_all[diverge][pool])]
        else:
            ordering = np.argsort(score)

        if group_filter:
            # "montre-moi celles qui different surtout en aim" : c'est le groupe
            # qui pese le plus dans la distance, donc `group_share` -- deja
            # calcule, rien a recalculer. Le tri par part evite une division :
            # a distance totale fixee, comparer les carres suffit.
            #
            # Le masque s'applique a l'ORDRE, pas a la liste finale. Couper apres
            # coup rognerait la liste au lieu de la remplir : c'est exactement le
            # piege du dedoublonnage par chanson, qui prend large avant de couper.
            stacked = np.stack([per_group_all[group] for group in GROUP_NAMES])
            dominant = np.argmax(stacked, axis=0)
            ordering = ordering[dominant[ordering] == GROUP_NAMES.index(group_filter)]
            if ordering.size == 0:
                return []

        # on prend large avant de dedoublonner, sinon retirer les doublons
        # rognerait la liste au lieu de la remplir
        shortlist = ordering[: min(ordering.size, k * 8 if max_per_song else k)]
        positions = candidates[shortlist]

        placeholders = ",".join("?" * len(positions))
        with self._db_lock:
            rows = {
                row["pos"]: row
                for row in self.db.execute(
                    f"SELECT * FROM maps WHERE pos IN ({placeholders})",
                    [int(p) for p in positions],
                )
            }

        matches: list[Match] = []
        seen: Counter = Counter()
        for position, local in zip(positions, shortlist):
            row = rows.get(int(position))
            if row is None:
                continue
            if max_per_song:
                key = song_key(row["artist"], row["title"])
                if seen[key] >= max_per_song:
                    continue
                seen[key] += 1

            distance_squared = float(total[local])
            groups = {g: float(per_group_all[g][local]) for g in GROUP_NAMES}
            differences, similarities = explain(
                target.vector, target.raw,
                self.vectors[position], self.raw_vectors[position],
            )
            matches.append(
                Match(
                    rank=len(matches) + 1,
                    checksum=row["checksum"],
                    beatmap_id=int(row["beatmap_id"]),
                    beatmapset_id=int(row["beatmapset_id"]),
                    artist=row["artist"] or "",
                    title=row["title"] or "",
                    creator=row["creator"] or "",
                    version=row["version"] or "",
                    stars=float(row["stars"]),
                    bpm=float(row["bpm"]),
                    length_s=float(row["length_s"]),
                    distance=float(np.sqrt(distance_squared)),
                    group_distance={g: float(np.sqrt(v)) for g, v in groups.items()},
                    group_share={
                        g: (v / distance_squared if distance_squared > 0 else 0.0)
                        for g, v in groups.items()
                    },
                    differences=differences,
                    similarities=similarities,
                    status=_row_get(row, "status", DUMP_STATUS),
                    playcount=_row_get(row, "playcount", None),
                )
            )
            if len(matches) >= k:
                break
        return matches

    def close(self) -> None:
        self.db.close()


class SearcherSet:
    """
    Les Searcher des differentes variantes de mods, charges a la demande.

    Un seul index tient en memoire virtuelle (les vecteurs sont mappes, pas
    charges), mais on evite quand meme d'ouvrir les cinq au demarrage : la
    plupart des sessions n'en utilisent qu'un ou deux.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._loaded: dict[str, Searcher] = {}

    def available(self) -> list[str]:
        return [name for name in VARIANTS if (self.root / name / DB_FILE).exists()]

    def get(self, variant: str = "nomod") -> Searcher:
        if variant not in self._loaded:
            directory = self.root / variant
            if not (directory / DB_FILE).exists():
                if variant == "nomod":
                    raise FileNotFoundError(
                        f"aucun index dans {directory} : lance scripts/build_all.py"
                    )
                return self.get("nomod")
            self._loaded[variant] = Searcher(directory)
        return self._loaded[variant]

    def for_mods(self, mods: str | None) -> tuple[str, Searcher]:
        wanted = variant_for_mods(mods)
        if wanted not in self.available():
            wanted = "nomod"
        return wanted, self.get(wanted)

    def close(self) -> None:
        for searcher in self._loaded.values():
            searcher.close()
        self._loaded.clear()
