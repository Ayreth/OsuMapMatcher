#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
features.py - LE coeur du projet.

`featurize(osu_file_bytes)` est utilisee a l'identique pour le corpus batch et
pour la map live. Toute divergence entre les deux rend l'ensemble faux, donc :

  * un seul chemin de code, aucune branche "batch" / "live" ;
  * le vecteur a une taille fixe et un ordre stable, definis par FEATURE_SPEC ;
  * `feature_vector()` lit les valeurs PAR NOM depuis FEATURE_NAMES, jamais dans
    l'ordre d'insertion du dict -> une derive d'ordre est structurellement
    impossible ;
  * tout echec renvoie None avec une raison, aucune exception ne remonte.

Trois groupes de features :
  A. metadonnees et parametres, parses directement dans le .osu
  B. profil des series de strains rosu-pp (la forme de la map)
  C. geometrie maison sur [HitObjects] (spacing normalise, angles, velocite)

Note rosu-pp-py 4.0.2 : en mode std, `rhythm` et `reading` sont None, cote
attributs comme cote strains. Le groupe "rhythm" est donc reconstruit a partir
des delta-times du groupe C, des BPM, et de la serie `flashlight` (peuplee meme
sans le mod FL, et qui se comporte comme un proxy de densite spatio-temporelle).
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np
import rosu_pp_py as rosu

log = logging.getLogger(__name__)

# --- constantes de modelisation ----------------------------------------------

MIN_HIT_OBJECTS = 20
MIN_DURATION_S = 20.0
#: au-dela de ce trou entre deux objets on considere qu'il n'y a pas de
#: continuite de mouvement (break, fin de kiai) : la paire est exclue des
#: statistiques de spacing / velocite / angle.
MAX_GAP_MS = 1000.0

#: bits du champ `type` d'un hit object
_TYPE_CIRCLE = 1 << 0
_TYPE_SLIDER = 1 << 1
_TYPE_SPINNER = 1 << 3

#: taux d'horloge des mods de vitesse
_RATE_MODS = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}
#: multiplicateurs de CS (les autres reglages viennent de rosu, deja mod-aware)
_CS_MODS = {"HR": 1.3, "EZ": 0.5}

#: statistiques calculees sur chaque serie de strains.
#: `mean` et `std` ont ete retires : mesures sur 2000 maps du dump, ils
#: correlent a r > 0.99 avec `p50` et avec le couple (`cv`, `p99`). Les garder
#: revenait a compter deux fois la meme dimension dans la distance.
STRAIN_STATS = (
    "p10", "p25", "p50", "p75", "p90", "p99",
    "cv", "skew", "spike", "hot",
)


def _strain_names(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}_{stat}" for stat in STRAIN_STATS)


# --- definition du vecteur ----------------------------------------------------

# Retirees apres mesure sur le dump, toutes redondantes a r > 0.99 :
#   great_hit_window (= 80 - 6*od), ratio_circles (= 1 - sliders - spinners),
#   total_time_s (drain_time_s + break_ratio le reconstruisent),
#   meter (4 dans 97 % des maps, aucune variance exploitable).
# `stars` est passee en metadonnee : elle correle a 0.99 avec d_aim et elle sert
# de filtre (bande de star rating), pas de dimension de similarite.
_PARAMS = (
    "cs", "hp", "od", "ar", "slider_multiplier", "slider_tick_rate",
    "n_objects", "ratio_sliders", "ratio_spinners", "max_combo",
    "n_large_ticks", "drain_time_s", "break_ratio", "object_density",
)
_RYTHME = (
    "bpm_median", "bpm_min", "bpm_max", "bpm_n_changes", "bpm_range_ratio",
    "beat_divisor", "dt_p10", "dt_p25", "dt_p50", "dt_p75", "dt_p90",
    "dt_cv", "dt_frac_lt100", "dt_frac_lt150",
)
_AIM_ATTRS = (
    "d_aim", "d_slider_factor", "d_aim_difficult_strain_count",
    "d_aim_difficult_slider_count", "d_aim_top_weighted_slider_factor",
    "aim_speed_ratio", "aimns_over_aim",
)
_SPEED_ATTRS = (
    "d_speed", "d_speed_note_count", "d_speed_difficult_strain_count",
    "d_speed_top_weighted_slider_factor",
)
_SPACING = (
    "sp_mean", "sp_p25", "sp_p50", "sp_p75", "sp_p90", "sp_cv",
    "ang_0_30", "ang_30_60", "ang_60_90", "ang_90_120", "ang_120_150",
    "ang_150_180",
    "vel_mean", "vel_p25", "vel_p50", "vel_p75", "vel_p90",
)

#: (nom, groupe de ponderation). L'ordre de ce tuple EST l'ordre du vecteur.
FEATURE_SPEC: tuple[tuple[str, str], ...] = (
    tuple((n, "parameters") for n in _PARAMS)
    + tuple((n, "rhythm") for n in _RYTHME)
    + tuple((n, "rhythm") for n in _strain_names("fl"))
    + tuple((n, "aim") for n in _strain_names("aim"))
    + tuple((n, "aim") for n in _strain_names("aimns"))
    + tuple((n, "aim") for n in _AIM_ATTRS)
    + tuple((n, "speed") for n in _strain_names("speed"))
    + tuple((n, "speed") for n in _SPEED_ATTRS)
    + tuple((n, "spacing") for n in _SPACING)
)

FEATURE_NAMES: tuple[str, ...] = tuple(name for name, _ in FEATURE_SPEC)
FEATURE_GROUPS: dict[str, str] = {name: group for name, group in FEATURE_SPEC}

#: empreinte du jeu de features, a echanger entre un client et une API distante.
#: Un vecteur calcule avec un autre FEATURE_NAMES donne des resultats plausibles
#: et faux -- c'est exactement le genre de panne qui ne se voit pas. Elle change
#: des qu'une colonne est ajoutee, retiree ou deplacee, c'est-a-dire au moment
#: ou tous les index sur disque deviennent caducs de toute facon.
FEATURE_VERSION: str = hashlib.sha256(
    "|".join(FEATURE_NAMES).encode("utf-8")
).hexdigest()[:12]
GROUP_NAMES: tuple[str, ...] = ("aim", "speed", "rhythm", "spacing", "parameters")

assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES)), "noms de features dupliques"
assert set(FEATURE_GROUPS.values()) == set(GROUP_NAMES), "groupe inconnu dans FEATURE_SPEC"

#: colonnes descriptives et colonnes de filtrage, jamais dans le vecteur de
#: distance. `stars` est ici : c'est une contrainte (bande de difficulte), pas
#: une dimension de ressemblance.
META_NAMES: tuple[str, ...] = (
    "checksum", "beatmap_id", "beatmapset_id",
    "title", "artist", "creator", "version", "stars",
)

#: indices des colonnes de chaque groupe, precalcules pour la ponderation
GROUP_INDICES: dict[str, np.ndarray] = {
    group: np.array(
        [i for i, name in enumerate(FEATURE_NAMES) if FEATURE_GROUPS[name] == group],
        dtype=np.int64,
    )
    for group in GROUP_NAMES
}


@dataclass(frozen=True)
class Mods:
    """
    Mods actifs, reduits a ce qui change reellement les features.

    rosu-pp applique lui-meme les mods a la difficulte et aux strains ; il ne
    reste donc a gerer de notre cote que ce qu'on calcule nous-memes :
    le taux d'horloge (qui comprime tous les temps) et le CS (qui sert a
    normaliser le spacing). AR, OD et HP nous reviennent deja mod-aware.

    Un acronyme inconnu est ignore, jamais une erreur : les mods viennent
    d'osu! en direct et on ne veut pas qu'un mod recent casse la recherche.
    """
    acronyms: str = ""
    clock_rate: float = 1.0
    cs_multiplier: float = 1.0

    @property
    def active(self) -> bool:
        return bool(self.acronyms) or self.clock_rate != 1.0

    @classmethod
    def parse(cls, spec: Any = None, rate: float | None = None) -> "Mods":
        if isinstance(spec, Mods):
            return spec
        if spec is None:
            acronyms = ""
        elif isinstance(spec, str):
            acronyms = spec
        else:
            try:
                acronyms = "".join(str(item) for item in spec)
            except TypeError:
                acronyms = ""
        acronyms = "".join(ch for ch in acronyms.upper() if ch.isalpha())
        pairs = {acronyms[i:i + 2] for i in range(0, len(acronyms) - 1, 2)}

        clock_rate = 1.0
        for mod, value in _RATE_MODS.items():
            if mod in pairs:
                clock_rate = value
                break
        # osu!lazer autorise des taux libres : ils priment sur l'acronyme
        if isinstance(rate, (int, float)) and rate > 0 and abs(rate - 1.0) > 1e-9:
            clock_rate = float(rate)

        cs_multiplier = 1.0
        for mod, value in _CS_MODS.items():
            if mod in pairs:
                cs_multiplier = value
                break
        return cls(acronyms=acronyms, clock_rate=clock_rate, cs_multiplier=cs_multiplier)

    def rosu_kwargs(self) -> dict[str, Any]:
        """Seuls les arguments non triviaux sont passes, pour que le chemin
        sans mods reste rigoureusement identique a ce qui a bati le corpus."""
        kwargs: dict[str, Any] = {}
        if self.acronyms:
            kwargs["mods"] = self.acronyms
        if self.clock_rate != 1.0:
            kwargs["clock_rate"] = self.clock_rate
        return kwargs

    def apply_cs(self, circle_size: float) -> float:
        return min(circle_size * self.cs_multiplier, 10.0)


NO_MODS = Mods()

#: Un corpus par combinaison de mods.
#:
#: Comparer une map jouee en DT a un corpus nomod n'a pas de sens : accelerée
#: elle vaut douze etoiles, et on irait alors chercher des maps qui valent douze
#: etoiles SANS mods, c'est-a-dire a peu pres rien. Mesure sur BLUE ENCOUNT -
#: HOPE : la distance du meilleur resultat passe de 1.57 en nomod a 6.98 en
#: DTHR contre le corpus nomod. Chaque variante a donc son propre corpus.
VARIANTS: tuple[str, ...] = ("nomod", "DT", "HR", "DTHR", "HT")


def variant_for_mods(mods: Any = None) -> str:
    """
    Variante de corpus a utiliser pour ces mods, avec repli sur la plus proche.

    "HDDT" -> "DT", "NC" -> "DT", "HDHRDT" -> "DTHR". Les mods sans effet sur
    les features (HD, NF, SD, PF, SO) sont ignores. Une combinaison sans corpus
    dedie retombe sur ce qui existe, quitte a laisser tomber une partie des
    mods : mieux vaut un resultat legerement approche que pas de resultat.
    """
    parsed = Mods.parse(mods)
    text = parsed.acronyms
    pairs = {text[i:i + 2] for i in range(0, len(text) - 1, 2)}

    rate = "DT" if pairs & {"DT", "NC"} else ("HT" if pairs & {"HT", "DC"} else "")
    hard = "HR" if "HR" in pairs else ""
    for candidate in (rate + hard, rate, hard, "nomod"):
        if candidate in VARIANTS:
            return candidate
    return "nomod"


@dataclass(frozen=True)
class FeatureResult:
    """Resultat detaille : soit `features`, soit `skip_reason`, jamais les deux."""
    features: dict[str, Any] | None
    skip_reason: str | None


# --- utilitaires numeriques ---------------------------------------------------

#: toutes les features sont des grandeurs physiques bornees ; ce plafond garantit
#: qu'aucune valeur ne deborde en float32 (limite ~3.4e38) au moment d'ecrire le
#: parquet. Sans lui, un seul point de timing aberrant produit un `inf`.
MAX_FEATURE_VALUE = 1e9

#: sous ce seuil un denominateur est traite comme nul : diviser par 1e-300
#: fabrique des valeurs astronomiques qui ecraseraient toutes les autres colonnes
MIN_DENOMINATOR = 1e-12


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    if not den or abs(den) < MIN_DENOMINATOR:
        return default
    return float(num / den)


def _finite(value: Any, default: float = 0.0) -> float:
    """Tout ce qui sort d'ici est un float fini et borne : ni None, ni NaN, ni inf."""
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return max(-MAX_FEATURE_VALUE, min(MAX_FEATURE_VALUE, out))


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    if not len(values):
        return 0.0
    vals = np.asarray(values, dtype=np.float64)
    wts = np.asarray(weights, dtype=np.float64)
    order = np.argsort(vals)
    vals, wts = vals[order], wts[order]
    total = wts.sum()
    if total <= 0:
        return float(np.median(vals))
    idx = int(np.searchsorted(np.cumsum(wts), total / 2.0))
    return float(vals[min(idx, vals.size - 1)])


def _quantile_stats(arr: np.ndarray, prefix: str, qs: Sequence[int]) -> dict[str, float]:
    """Quantiles nommes `{prefix}_p{q}`. Renvoie des zeros si arr est vide."""
    if arr.size == 0:
        return {f"{prefix}_p{q}": 0.0 for q in qs}
    values = np.percentile(arr, qs)
    return {f"{prefix}_p{q}": float(v) for q, v in zip(qs, values)}


def _strain_profile(series: Iterable[float] | None, prefix: str) -> dict[str, float]:
    """
    12 statistiques decrivant la FORME d'une serie de strains.

    Les zeros de tete et de queue sont retires (sections avant le premier objet
    et apres le dernier, qui ne decrivent pas la map). Les zeros interieurs sont
    conserves : un break est une vraie caracteristique structurelle.
    """
    empty = {f"{prefix}_{stat}": 0.0 for stat in STRAIN_STATS}
    if series is None:
        return empty
    arr = np.asarray(list(series), dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    nonzero = np.nonzero(arr > 0)[0]
    if nonzero.size == 0:
        return empty
    arr = arr[nonzero[0]: nonzero[-1] + 1]

    p10, p25, p50, p75, p90, p99 = np.percentile(arr, [10, 25, 50, 75, 90, 99])
    mean = float(arr.mean())
    std = float(arr.std())
    return {
        f"{prefix}_p10": float(p10),
        f"{prefix}_p25": float(p25),
        f"{prefix}_p50": float(p50),
        f"{prefix}_p75": float(p75),
        f"{prefix}_p90": float(p90),
        f"{prefix}_p99": float(p99),
        # coefficient de variation : dispersion sans l'echelle
        f"{prefix}_cv": _safe_div(std, mean),
        # asymetrie : positif = quelques pics isoles sur un fond calme
        f"{prefix}_skew": _safe_div(float(((arr - mean) ** 3).mean()), std ** 3),
        # p99/p50 : map a spike unique (eleve) vs map consistante (proche de 1)
        f"{prefix}_spike": _safe_div(float(p99), float(p50)),
        # proportion de sections dans le haut du spectre : densite des passages durs
        f"{prefix}_hot": float((arr >= 0.8 * p99).mean()) if p99 > 0 else 0.0,
    }


# --- parsing du .osu ----------------------------------------------------------

@dataclass
class HitObjects:
    x: np.ndarray
    y: np.ndarray
    t: np.ndarray
    n_circles: int
    n_sliders: int
    n_spinners: int
    end_time: float

    def __len__(self) -> int:
        return int(self.t.size)

    def rescale(self, clock_rate: float) -> "HitObjects":
        """Applique un taux d'horloge (DT, HT, taux libre de lazer)."""
        return HitObjects(
            x=self.x, y=self.y, t=self.t / clock_rate,
            n_circles=self.n_circles, n_sliders=self.n_sliders,
            n_spinners=self.n_spinners, end_time=self.end_time / clock_rate,
        )


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _key_values(lines: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _num(mapping: dict[str, str], key: str, default: float) -> float:
    return _finite(mapping.get(key), default)


def _parse_hit_objects(lines: Sequence[str]) -> HitObjects:
    xs: list[float] = []
    ys: list[float] = []
    ts: list[float] = []
    n_circles = n_sliders = n_spinners = 0
    end_time = 0.0

    for line in lines:
        parts = line.split(",")
        if len(parts) < 4:
            continue  # ligne tronquee, on saute
        try:
            x = float(parts[0])
            y = float(parts[1])
            t = float(parts[2])
            kind = int(float(parts[3]))
        except ValueError:
            continue
        xs.append(x)
        ys.append(y)
        ts.append(t)
        obj_end = t
        if kind & _TYPE_SPINNER:
            n_spinners += 1
            if len(parts) >= 6:
                obj_end = _finite(parts[5], t)
        elif kind & _TYPE_SLIDER:
            n_sliders += 1
        elif kind & _TYPE_CIRCLE:
            n_circles += 1
        else:
            # note longue de mania, ou type inconnu : compte comme un cercle
            n_circles += 1
        end_time = max(end_time, obj_end)

    return HitObjects(
        x=np.asarray(xs, dtype=np.float64),
        y=np.asarray(ys, dtype=np.float64),
        t=np.asarray(ts, dtype=np.float64),
        n_circles=n_circles,
        n_sliders=n_sliders,
        n_spinners=n_spinners,
        end_time=end_time,
    )


def _parse_timing(
    lines: Sequence[str], end_time: float, *, clock_rate: float = 1.0
) -> dict[str, float]:
    """
    BPM ponderes par la duree, depuis les points de timing non herites.

    Un point est non herite quand son 7e champ vaut 1 ; sur les tres vieux
    formats le champ est absent et un beatLength positif fait foi.
    """
    times: list[float] = []
    bpms: list[float] = []

    for line in lines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        time = _finite(parts[0], math.nan)
        beat_length = _finite(parts[1], math.nan)
        if not math.isfinite(time) or not math.isfinite(beat_length):
            continue
        uninherited = _finite(parts[6], 1.0) if len(parts) > 6 else 1.0
        if uninherited < 1 or beat_length <= 0:
            continue  # point herite : c'est du SV, pas du BPM
        bpm = 60000.0 / beat_length * clock_rate
        # un beatLength denormal (vu en vrai dans le dump) donne un BPM infini :
        # on ecarte tout ce qui sort de la plage physiquement plausible
        if not math.isfinite(bpm) or not (1.0 <= bpm <= 10000.0):
            continue
        times.append(time / clock_rate)
        bpms.append(bpm)

    if not bpms:
        return {"bpm_median": 0.0, "bpm_min": 0.0, "bpm_max": 0.0,
                "bpm_n_changes": 0.0, "bpm_range_ratio": 1.0}

    order = np.argsort(times)
    times_arr = np.asarray(times)[order]
    bpms_arr = np.asarray(bpms)[order]

    bounds = np.append(times_arr[1:], max(end_time, times_arr[-1]))
    durations = np.clip(bounds - times_arr, 0.0, None)

    bpm_min = float(bpms_arr.min())
    bpm_max = float(bpms_arr.max())
    return {
        "bpm_median": _weighted_median(bpms_arr, durations),
        "bpm_min": bpm_min,
        "bpm_max": bpm_max,
        # nombre de vrais changements de tempo, pas de points redondants
        "bpm_n_changes": float(np.count_nonzero(np.diff(bpms_arr) != 0)),
        "bpm_range_ratio": _safe_div(bpm_max, bpm_min, 1.0),
    }


def _parse_breaks(lines: Sequence[str]) -> float:
    """Duree totale des breaks en ms, depuis les evenements de type 2."""
    total = 0.0
    for line in lines:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        head = parts[0].strip().lower()
        if head not in ("2", "break"):
            continue
        start = _finite(parts[1], math.nan)
        end = _finite(parts[2], math.nan)
        if math.isfinite(start) and math.isfinite(end) and end > start:
            total += end - start
    return total


# --- groupe C : geometrie -----------------------------------------------------

def _geometry(ho: HitObjects, circle_size: float) -> dict[str, float]:
    """
    Spacing normalise par le rayon de hit, angles, velocite requise.

    La normalisation par le rayon est ce qui rend un CS4 et un CS5 comparables :
    une distance de 100 px n'a pas la meme difficulte selon la taille des
    cercles. Les courbes de sliders sont ignorees (v1) : on ne regarde que les
    positions de depart des objets.
    """
    radius = max(54.4 - 4.48 * circle_size, 1.0)  # rayon osu! en px

    dx = np.diff(ho.x)
    dy = np.diff(ho.y)
    dt = np.diff(ho.t)
    dist = np.hypot(dx, dy) / radius

    # On ne garde que les paires reellement enchainees. Le seuil est adaptatif :
    # sur une map tres lente (Easy a 0.6 objet/s) l'ecart normal depasse la
    # seconde, et un seuil fixe excluait alors 100 % des paires -- le spacing
    # sortait a zero sur toute la ligne.
    positive = dt[dt > 0]
    gap_limit = MAX_GAP_MS
    if positive.size:
        gap_limit = max(MAX_GAP_MS, 4.0 * float(np.median(positive)))
    ok = (dt > 0) & (dt <= gap_limit)
    spacing = dist[ok]
    gaps = dt[ok]

    out: dict[str, float] = {}
    out.update(_quantile_stats(spacing, "sp", (25, 50, 75, 90)))
    mean_sp = float(spacing.mean()) if spacing.size else 0.0
    out["sp_mean"] = mean_sp
    out["sp_cv"] = _safe_div(float(spacing.std()) if spacing.size else 0.0, mean_sp)

    # velocite requise : rayons parcourus par seconde
    velocity = spacing / (gaps / 1000.0) if spacing.size else np.empty(0)
    out.update(_quantile_stats(velocity, "vel", (25, 50, 75, 90)))
    out["vel_mean"] = float(velocity.mean()) if velocity.size else 0.0

    # angles au sommet de chaque triplet d'objets consecutifs
    bin_names = ("ang_0_30", "ang_30_60", "ang_60_90",
                 "ang_90_120", "ang_120_150", "ang_150_180")
    triple = ok[:-1] & ok[1:]  # les deux paires du triplet doivent etre valides
    if np.any(triple):
        idx = np.nonzero(triple)[0]  # sommet = objet idx+1
        v1x, v1y = -dx[idx], -dy[idx]          # sommet -> objet precedent
        v2x, v2y = dx[idx + 1], dy[idx + 1]    # sommet -> objet suivant
        n1 = np.hypot(v1x, v1y)
        n2 = np.hypot(v2x, v2y)
        valid = (n1 > 0) & (n2 > 0)
        if np.any(valid):
            cos = (v1x[valid] * v2x[valid] + v1y[valid] * v2y[valid]) / (n1[valid] * n2[valid])
            angles = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
            counts, _ = np.histogram(angles, bins=[0, 30, 60, 90, 120, 150, 180.001])
            fractions = counts / counts.sum() if counts.sum() else counts
            out.update({name: float(v) for name, v in zip(bin_names, fractions)})
        else:
            out.update({name: 0.0 for name in bin_names})
    else:
        out.update({name: 0.0 for name in bin_names})

    # delta-times : le substitut au groupe `rhythm` absent de rosu-pp en std
    out.update(_quantile_stats(gaps, "dt", (10, 25, 50, 75, 90)))
    mean_dt = float(gaps.mean()) if gaps.size else 0.0
    out["dt_cv"] = _safe_div(float(gaps.std()) if gaps.size else 0.0, mean_dt)
    out["dt_frac_lt100"] = float((gaps < 100).mean()) if gaps.size else 0.0
    out["dt_frac_lt150"] = float((gaps < 150).mean()) if gaps.size else 0.0
    return out


# --- API principale -----------------------------------------------------------

def featurize_detailed(raw: bytes, mods: Any = None) -> FeatureResult:
    """Version instrumentee de `featurize` : porte la raison du skip."""
    try:
        return _featurize(raw, Mods.parse(mods))
    except Exception as exc:  # aucune exception ne doit remonter au batch
        log.debug("featurize a leve %s: %s", type(exc).__name__, exc)
        return FeatureResult(None, f"exception:{type(exc).__name__}")


def featurize(raw: bytes, mods: Any = None) -> dict[str, Any] | None:
    """
    Contrat de la phase 2 : un .osu en entree, un dict plat ou None en sortie.

    Renvoie None (et logue la raison) si le mode n'est pas std, si le fichier
    est illisible ou tronque, s'il compte moins de 20 objets, ou s'il dure
    moins de 20 secondes. Ne leve jamais.

    `mods` accepte "DT", "HDDT", une liste d'acronymes, ou un objet Mods. Sans
    mods, le resultat est bit pour bit celui qui a servi a batir le corpus.
    """
    result = featurize_detailed(raw, mods)
    if result.features is None:
        log.info("skip: %s", result.skip_reason)
    return result.features


def _featurize(raw: bytes, mods: Mods = NO_MODS) -> FeatureResult:
    if not raw:
        return FeatureResult(None, "empty_file")

    text = raw.decode("utf-8-sig", errors="replace")
    if "osu file format v" not in text[:200]:
        return FeatureResult(None, "bad_header")

    sections = _split_sections(text)
    general = _key_values(sections.get("General", []))
    metadata = _key_values(sections.get("Metadata", []))
    difficulty = _key_values(sections.get("Difficulty", []))
    editor = _key_values(sections.get("Editor", []))

    if int(_num(general, "Mode", 0.0)) != 0:
        return FeatureResult(None, "not_std")

    hit_lines = sections.get("HitObjects")
    if not hit_lines:
        return FeatureResult(None, "no_hitobjects")

    ho = _parse_hit_objects(hit_lines)
    if len(ho) < MIN_HIT_OBJECTS:
        return FeatureResult(None, "too_few_objects")

    # Les mods de vitesse comprimment tous les temps. On l'applique une seule
    # fois, ici, et tout ce qui en decoule (ecarts, velocite, densite, BPM,
    # duree) suit automatiquement.
    if mods.clock_rate != 1.0:
        ho = ho.rescale(mods.clock_rate)

    total_time_s = (ho.end_time - float(ho.t[0])) / 1000.0
    if total_time_s < MIN_DURATION_S:
        return FeatureResult(None, "too_short")

    # --- rosu-pp : difficulte et strains --------------------------------------
    try:
        beatmap = rosu.Beatmap(bytes=raw)
        calculator = rosu.Difficulty(**mods.rosu_kwargs())
        attrs = calculator.calculate(beatmap)
        strains = calculator.strains(beatmap)
    except Exception as exc:
        return FeatureResult(None, f"rosu:{type(exc).__name__}")

    if strains.aim is None:
        # filet de securite : rosu ne peuple pas les strains std -> pas une map std
        return FeatureResult(None, "rosu_not_std")

    def attr(name: str, default: float = 0.0) -> float:
        return _finite(getattr(attrs, name, None), default)

    # --- assemblage -----------------------------------------------------------
    circle_size = mods.apply_cs(_num(difficulty, "CircleSize", 5.0))
    break_ms = _parse_breaks(sections.get("Events", [])) / mods.clock_rate
    drain_time_s = max(total_time_s - break_ms / 1000.0, 0.0)
    n_objects = float(len(ho))

    # OD deduite de la fenetre de hit renvoyee par rosu : c'est exactement la
    # meme grandeur (r = 0.9999 mesure sur le corpus), mais deja corrigee des
    # mods, alors que la valeur ecrite dans le .osu ne l'est pas.
    great_window = attr("great_hit_window", math.nan)
    if math.isfinite(great_window) and great_window > 0:
        overall_difficulty = (80.0 - great_window) / 6.0
    else:
        overall_difficulty = _num(difficulty, "OverallDifficulty", 5.0)

    feats: dict[str, Any] = {}

    # groupe A : parametres
    feats.update({
        "cs": circle_size,
        "hp": attr("hp", _num(difficulty, "HPDrainRate", 5.0)),
        "od": overall_difficulty,
        "ar": attr("ar", _num(difficulty, "ApproachRate",
                              _num(difficulty, "OverallDifficulty", 5.0))),
        "slider_multiplier": _num(difficulty, "SliderMultiplier", 1.4),
        "slider_tick_rate": _num(difficulty, "SliderTickRate", 1.0),
        "n_objects": n_objects,
        "ratio_sliders": _safe_div(ho.n_sliders, n_objects),
        "ratio_spinners": _safe_div(ho.n_spinners, n_objects),
        "max_combo": attr("max_combo"),
        "n_large_ticks": attr("n_large_ticks"),
        "drain_time_s": drain_time_s,
        "break_ratio": _safe_div(break_ms / 1000.0, total_time_s),
        "object_density": _safe_div(n_objects, drain_time_s),
    })

    # groupe A bis : timing
    feats.update(_parse_timing(sections.get("TimingPoints", []), ho.end_time,
                               clock_rate=mods.clock_rate))
    feats["beat_divisor"] = _num(editor, "BeatDivisor", 4.0)

    # groupe C : geometrie (fournit aussi les dt_* du groupe rythme)
    feats.update(_geometry(ho, circle_size))

    # groupe B : profils de strains
    feats.update(_strain_profile(strains.aim, "aim"))
    feats.update(_strain_profile(strains.aim_no_sliders, "aimns"))
    feats.update(_strain_profile(strains.speed, "speed"))
    feats.update(_strain_profile(strains.flashlight, "fl"))

    # groupe B bis : attributs scalaires et ratios croises
    feats.update({
        "d_aim": attr("aim"),
        "d_slider_factor": attr("slider_factor"),
        "d_aim_difficult_strain_count": attr("aim_difficult_strain_count"),
        "d_aim_difficult_slider_count": attr("aim_difficult_slider_count"),
        "d_aim_top_weighted_slider_factor": attr("aim_top_weighted_slider_factor"),
        "d_speed": attr("speed"),
        "d_speed_note_count": attr("speed_note_count"),
        "d_speed_difficult_strain_count": attr("speed_difficult_strain_count"),
        "d_speed_top_weighted_slider_factor": attr("speed_top_weighted_slider_factor"),
        "aim_speed_ratio": _safe_div(attr("aim"), attr("speed")),
        # poids des sliders dans la difficulte d'aim
        "aimns_over_aim": _safe_div(feats["aimns_p50"], feats["aim_p50"]),
    })

    missing = set(FEATURE_NAMES) - set(feats)
    extra = set(feats) - set(FEATURE_NAMES)
    if missing or extra:  # bug de programmation, pas une map invalide
        raise AssertionError(f"features manquantes={sorted(missing)} en trop={sorted(extra)}")

    # tout est fini, borne, exploitable par le scaler
    for name in FEATURE_NAMES:
        feats[name] = _finite(feats[name])

    feats.update({
        "checksum": hashlib.md5(raw).hexdigest(),
        "beatmap_id": int(_num(metadata, "BeatmapID", 0.0)),
        "beatmapset_id": int(_num(metadata, "BeatmapSetID", 0.0)),
        "title": metadata.get("Title", ""),
        "artist": metadata.get("Artist", ""),
        "creator": metadata.get("Creator", ""),
        "version": metadata.get("Version", ""),
        "stars": attr("stars"),
    })
    return FeatureResult(feats, None)


# --- passage au vecteur -------------------------------------------------------

def feature_vector(features: dict[str, Any]) -> np.ndarray:
    """
    Vecteur float32 dans l'ordre de FEATURE_NAMES.

    La lecture se fait par nom : l'ordre d'insertion du dict n'a aucune
    influence, et une cle absente echoue bruyamment plutot que de decaler
    silencieusement toutes les colonnes suivantes.
    """
    try:
        values = [features[name] for name in FEATURE_NAMES]
    except KeyError as exc:
        raise KeyError(f"feature absente du dict : {exc.args[0]}") from exc
    vector = np.asarray(values, dtype=np.float32)
    assert vector.shape == (len(FEATURE_NAMES),), "taille de vecteur inattendue"
    return vector


def feature_matrix(rows: Iterable[dict[str, Any]]) -> np.ndarray:
    stack = [feature_vector(row) for row in rows]
    if not stack:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float32)
    return np.vstack(stack)
