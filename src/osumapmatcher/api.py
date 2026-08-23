#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api.py - API HTTP publique, sans etat et sans tosu, deployable sur un serveur.

server.py parle a tosu et sert une page ; cette API-la ne sait rien de la
machine du joueur. Elle recoit un checksum, un dict de features deja calcule ou
un .osu brut, et renvoie les voisins.

Les deux hotes montent le MEME routeur (`build_router`) : le client parle un
seul protocole qu'il tape en local ou a distance, seule l'URL change. C'est ce
qui rend le repli distant -> local possible sans deuxieme implementation.

Trois facons de poser la question, par cout croissant pour le serveur :

  1. checksum d'une map du corpus  -> zero CPU, le vecteur est deja sur disque
  2. dict de features deja calcule -> zero CPU, le client a fait le travail
  3. fichier .osu brut             -> le serveur featurise (~20 ms)

Le cas 3 commence quand meme par chercher le md5 du fichier dans le corpus : le
checksum d'une map EST le md5 de son .osu, donc une map ranked envoyee telle
quelle ne coute rien non plus. Seules les maps non soumises ou editees sont
reellement featurisees.

Une quatrieme forme part de PLUSIEURS maps : `POST /similar/multi` prend une
liste de checksums et de beatmap_ids, en fait un centroide et rend ses voisins.
La reponse garde la meme forme que les autres ; seul `query` change de sens et
decrit l'ensemble des graines.

Les reglages, eux, vivent tous dans `SearchOptions` et `options_from_query`, une
seule fois : c'est ce qui fait qu'un ajout arrive d'un coup sur `/similar`,
`/similar/multi` et `/similar/file` au lieu d'etre recopie trois fois. Le
dernier en date est `steer`, la direction -- "comme celle-ci, mais plus dense" --
qui s'ecrit `object_density:+0.3,sp_p50:-0.5` dans une query string et comme un
dict en JSON.

Garde-fous, tous reglables par variable d'environnement : CORS, jeton optionnel,
seau a jetons par IP, taille d'upload bornee, semaphore sur le travail CPU. Et
surtout le controle de version du jeu de features : un vecteur calcule avec un
autre FEATURE_NAMES produirait des resultats plausibles et faux, donc il est
refuse (409) au lieu d'etre servi.

Variables d'environnement :

    OSUMAPMATCHER_DATA            dossier de donnees (voir paths.py)
    OSUMAPMATCHER_API_VARIANTS    variantes servies, ex. "nomod,DT" (defaut : toutes)
    OSUMAPMATCHER_API_CORS        origines autorisees, ex. "https://exemple.com"
                            (defaut "*", l'API est publique et en lecture seule)
    OSUMAPMATCHER_API_TOKEN       si non vide, exige Authorization: Bearer <jeton>
    OSUMAPMATCHER_API_RATE        requetes par minute et par IP (defaut 120, 0 = off)
    OSUMAPMATCHER_API_BURST       taille du seau, en requetes (defaut 60)
    OSUMAPMATCHER_API_FILE_COST   cout d'un upload en jetons (defaut 5)
    OSUMAPMATCHER_API_MAX_UPLOAD  taille max d'un .osu en octets (defaut 4000000)
    OSUMAPMATCHER_API_CONCURRENCY travaux CPU simultanes (defaut : nb de coeurs)
    OSUMAPMATCHER_API_TRUST_PROXY 1 si un reverse proxy pose X-Forwarded-For
    OSUMAPMATCHER_API_CACHE       featurisations gardees en cache (defaut 256)
    OSUMAPMATCHER_API_DOCS        0 pour masquer /docs et /openapi.json
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import threading
import time
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import (
    APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from .features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    GROUP_NAMES,
    VARIANTS,
    FeatureResult,
    featurize_detailed,
    variant_for_mods,
)
from .index import (
    DEFAULT_AVOID_STRENGTH,
    DEFAULT_STAR_RANGE,
    MAX_AVOID_STRENGTH,
    MAX_STEER,
    STEER_AXES,
    QueryTarget,
    Searcher,
    SearcherSet,
    combine_targets,
    normalise_steer,
)
from .paths import index_root

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

API_PREFIX = "/api/v1"
#: nombre de seaux a jetons gardes en memoire avant nettoyage
_MAX_BUCKETS = 20_000
#: graines acceptees par /similar/multi. Chacune ne coute qu'une lecture SQL,
#: mais un centroide de cinquante maps ne veut plus rien dire : la borne est la
#: pour l'utilisateur autant que pour le serveur.
MAX_SEEDS = 20
#: au-dela de cet ecart d'etoiles entre graines, le centroide est un compromis
#: que personne n'a demande -- mesure : des graines a 3,0 et 8,0 etoiles sortent
#: des resultats a 6 etoiles. On le dit dans `warnings` plutot que de le taire.
WIDE_SPAN_STARS = 2.0


# --- configuration ------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_num(name: str, default: float) -> float:
    try:
        return float(_env_str(name) or default)
    except ValueError:
        log.warning("%s illisible, valeur par defaut %s", name, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _env_str(name).split(",") if item.strip())


@dataclass(frozen=True)
class ApiConfig:
    """Tout ce qui distingue une API exposee d'un serveur local de confiance."""

    variants: tuple[str, ...] = ()          # () = toutes celles presentes sur disque
    cors_origins: tuple[str, ...] = ()      # () = pas de CORS du tout
    cors_regex: str = ""                    # motif d'origines, en plus de la liste
    token: str = ""
    rate_per_minute: float = 0.0            # 0 = pas de limitation
    burst: float = 60.0
    file_cost: float = 5.0
    max_upload: int = 4_000_000
    concurrency: int = 0                    # 0 = nombre de coeurs
    trust_proxy: bool = False
    cache_size: int = 256
    docs: bool = True

    @classmethod
    def from_env(cls) -> "ApiConfig":
        """Reglages d'un deploiement expose : limites actives par defaut."""
        return cls(
            variants=_env_list("OSUMAPMATCHER_API_VARIANTS"),
            cors_origins=_env_list("OSUMAPMATCHER_API_CORS") or ("*",),
            token=_env_str("OSUMAPMATCHER_API_TOKEN"),
            rate_per_minute=_env_num("OSUMAPMATCHER_API_RATE", 120.0),
            burst=_env_num("OSUMAPMATCHER_API_BURST", 60.0),
            file_cost=_env_num("OSUMAPMATCHER_API_FILE_COST", 5.0),
            max_upload=int(_env_num("OSUMAPMATCHER_API_MAX_UPLOAD", 4_000_000)),
            concurrency=int(_env_num("OSUMAPMATCHER_API_CONCURRENCY", 0)),
            trust_proxy=_env_bool("OSUMAPMATCHER_API_TRUST_PROXY", False),
            cache_size=int(_env_num("OSUMAPMATCHER_API_CACHE", 256)),
            docs=_env_bool("OSUMAPMATCHER_API_DOCS", True),
        )

    @classmethod
    def local(cls) -> "ApiConfig":
        """
        Reglages du serveur local : un seul utilisateur, aucune limite.

        Le CORS y sert quand meme : le counter tosu est servi par tosu, sur un
        autre port, et doit pouvoir interroger ce serveur-ci. On n'ouvre qu'aux
        origines de la machine -- une page distante n'a rien a faire dans
        l'index de quelqu'un.
        """
        return cls(cors_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")

    @property
    def workers(self) -> int:
        return self.concurrency or (os.cpu_count() or 4)


# --- limitation de debit ------------------------------------------------------

class RateLimiter:
    """
    Seau a jetons par client, en memoire.

    Volontairement sans Redis : une instance de cette API tient largement la
    charge d'une communaute osu!, et une dependance de plus a administrer sur le
    serveur de quelqu'un est un cout reel. Avec plusieurs workers uvicorn chaque
    processus a son propre seau, donc la limite effective est `rate x workers` --
    c'est documente dans deploy/README.md, ce n'est pas un accident.
    """

    def __init__(self, per_minute: float, burst: float) -> None:
        self.rate = max(0.0, per_minute) / 60.0
        self.capacity = max(1.0, burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.rate > 0.0

    def take(self, key: str, cost: float = 1.0) -> float:
        """Renvoie 0.0 si la requete passe, sinon le delai a attendre."""
        if not self.active:
            return 0.0
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > _MAX_BUCKETS:
                self._prune(now)
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                return (cost - tokens) / self.rate
            self._buckets[key] = (tokens - cost, now)
            return 0.0

    def _prune(self, now: float) -> None:
        """Jette les seaux revenus a plein : ils valent un client inconnu."""
        cutoff = now - self.capacity / self.rate if self.rate else 0.0
        self._buckets = {
            key: value for key, value in self._buckets.items() if value[1] > cutoff
        }


# --- service de recherche -----------------------------------------------------

class SearchService:
    """
    Enrobe un SearcherSet : verrou sqlite, semaphore CPU, cache de featurisation.

    Le verrou est par variante et pas global : deux recherches sur deux corpus
    differents ne se genent pas. Il est necessaire parce qu'une connexion sqlite
    ouverte en check_same_thread=False n'est pas reentrante, et que la recherche
    tourne dans le pool de threads pour ne pas bloquer la boucle asyncio.
    """

    def __init__(
        self,
        provider: Callable[[], SearcherSet | None],
        config: ApiConfig,
    ) -> None:
        self._provider = provider
        self.config = config
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._semaphore = asyncio.Semaphore(config.workers)
        self._cache: OrderedDict[tuple[str, str], FeatureResult] = OrderedDict()
        self._cache_guard = threading.Lock()
        self.stats: Counter = Counter()
        self.started_at = time.time()

    # -- acces aux index -------------------------------------------------------

    def searchers(self) -> SearcherSet:
        found = self._provider()
        if found is None:
            raise HTTPException(503, "Index not loaded yet.")
        return found

    def available(self) -> list[str]:
        found = self._provider()
        if found is None:
            return []
        offered = found.available()
        if self.config.variants:
            offered = [name for name in offered if name in self.config.variants]
        return offered

    def ready(self) -> bool:
        return bool(self.available())

    def pick(
        self, *, variant: str | None = None, mods: str = "", rate: float = 1.0
    ) -> tuple[str, Searcher, list[str]]:
        """
        Choisit le corpus a interroger et dit ce qu'on a du approximer.

        Le repli n'est jamais silencieux : si la variante demandee n'est pas
        servie, ou si le taux d'horloge n'est modelise par aucune variante, ca
        part dans `warnings` et le client peut le montrer a l'utilisateur.
        """
        offered = self.available()
        if not offered:
            raise HTTPException(503, "No index available on this server.")

        warnings: list[str] = []
        if variant:
            if variant not in VARIANTS:
                raise HTTPException(
                    400, f"Unknown variant {variant!r}. Known: {', '.join(VARIANTS)}."
                )
            wanted = variant
        else:
            wanted = variant_for_mods(mods)

        if wanted not in offered:
            warnings.append(
                f"variant {wanted!r} is not served here, falling back to 'nomod'"
            )
            wanted = "nomod"
        if wanted not in offered:  # meme nomod peut manquer sur un serveur partiel
            wanted = offered[0]

        # Les taux libres de lazer ne sont modelises par aucune variante : DT
        # c'est 1.5 et rien d'autre. Mieux vaut le dire que rendre une reponse
        # qui a l'air juste.
        if rate and abs(rate - 1.0) > 1e-9:
            expected = {"DT": 1.5, "DTHR": 1.5, "HT": 0.75}.get(wanted, 1.0)
            if abs(rate - expected) > 1e-9:
                warnings.append(
                    f"custom clock rate {rate:g}x is not modelled; "
                    f"answered against the {wanted!r} corpus ({expected:g}x)"
                )

        return wanted, self.searchers().get(wanted), warnings

    def _lock(self, variant: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(variant, threading.Lock())

    # -- travail lourd ---------------------------------------------------------

    async def featurize(self, raw: bytes, variant: str) -> FeatureResult:
        """
        Featurise avec les mods CANONIQUES de la variante, jamais les mods bruts.

        Meme regle que tosu.py : le corpus DT a ete bati avec "DT", donc un
        vecteur calcule en "HDDT" y serait compare avec un biais invisible.
        """
        key = (hashlib.md5(raw).hexdigest(), variant)
        cached = self._cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        canonical = None if variant == "nomod" else variant
        async with self._semaphore:
            result = await asyncio.to_thread(featurize_detailed, raw, canonical)
        self.stats["featurized"] += 1
        self._cache_put(key, result)
        return result

    async def search(
        self,
        variant: str,
        searcher: Searcher,
        target: QueryTarget,
        options: "SearchOptions",
        avoid: QueryTarget | None = None,
    ) -> list[Any]:
        lock = self._lock(variant)

        def run() -> list[Any]:
            with lock:
                return searcher.search(target, **options.search_kwargs(avoid))

        async with self._semaphore:
            try:
                matches = await asyncio.to_thread(run)
            except KeyError as exc:  # str(KeyError) rajoute des guillemets
                raise HTTPException(400, str(exc.args[0])) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        self.stats["searches"] += 1
        return matches

    async def in_corpus(self, variant: str, searcher: Searcher, checksum: str) -> bool:
        if not checksum:
            return False
        lock = self._lock(variant)

        def run() -> bool:
            with lock:
                return searcher.has_checksum(checksum)

        return await asyncio.to_thread(run)

    async def target_from_checksum(
        self, variant: str, searcher: Searcher, checksum: str
    ) -> QueryTarget | None:
        lock = self._lock(variant)

        def run() -> QueryTarget | None:
            with lock:
                return searcher.target_from_checksum(checksum)

        return await asyncio.to_thread(run)

    async def target_from_beatmap_id(
        self, variant: str, searcher: Searcher, beatmap_id: int
    ) -> QueryTarget | None:
        lock = self._lock(variant)

        def run() -> QueryTarget | None:
            with lock:
                return searcher.target_from_beatmap_id(beatmap_id)

        return await asyncio.to_thread(run)

    # -- cache -----------------------------------------------------------------

    def _cache_get(self, key: tuple[str, str]) -> FeatureResult | None:
        if self.config.cache_size <= 0:
            return None
        with self._cache_guard:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def _cache_put(self, key: tuple[str, str], value: FeatureResult) -> None:
        if self.config.cache_size <= 0:
            return
        with self._cache_guard:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.config.cache_size:
                self._cache.popitem(last=False)


# --- modeles de requete -------------------------------------------------------

class Weights(BaseModel):
    aim: float = Field(1.0, ge=0.0, le=2.0)
    speed: float = Field(1.0, ge=0.0, le=2.0)
    rhythm: float = Field(1.0, ge=0.0, le=2.0)
    spacing: float = Field(1.0, ge=0.0, le=2.0)
    parameters: float = Field(1.0, ge=0.0, le=2.0)


def parse_steer(raw: str) -> dict[str, float]:
    """
    Lit la forme compacte `object_density:+0.3,sp_p50:-0.5`.

    Une direction est un dict en JSON, mais une query string n'a pas de dict :
    quinze parametres `steer_object_density=...` seraient illisibles et
    obligeraient a en ajouter un a chaque nouvel axe. Une seule chaine tient
    dans une URL qu'on peut copier-coller.

    Ne valide QUE la forme. Les noms d'axes et les bornes appartiennent a
    `normalise_steer` dans index.py, seul endroit qui doit en decider -- et ses
    erreurs deviennent des 400 en passant par `Service.search`.
    """
    directions: dict[str, float] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        axis, separator, amount = item.partition(":")
        if not separator or not axis.strip():
            raise ValueError(
                f"steer item {item!r} should look like 'object_density:+0.3'."
            )
        try:
            directions[axis.strip()] = float(amount.strip())
        except ValueError:
            raise ValueError(
                f"steer amount {amount.strip()!r} is not a number."
            ) from None
    return directions


class SearchOptions(BaseModel):
    """Les memes reglages que la page locale, aux memes bornes."""

    k: int = Field(20, ge=1, le=100)
    star_range: float = Field(DEFAULT_STAR_RANGE, ge=0.0, le=10.0)
    length_min: float = Field(0.5, ge=0.05, le=1.0)
    length_max: float = Field(2.0, ge=1.0, le=20.0)
    exclude_same_set: bool = True
    max_per_song: int = Field(1, ge=0, le=10)
    diverge: str | None = None
    #: les maps non classees entrent dans le pool par defaut ; un client
    #: qui ne veut que du telechargeable normalement passe False
    allow_unranked: bool = True
    #: ne garder que les maps dont l'ecart vient surtout de ce groupe
    group_filter: str | None = None
    #: le contre-exemple, designe comme une map ordinaire : "comme celle-ci,
    #: mais pas comme celle-la". Deux facons de le nommer, comme pour la cible.
    avoid_checksum: str | None = None
    avoid_beatmap_id: int | None = None
    avoid_strength: float = Field(
        DEFAULT_AVOID_STRENGTH, ge=0.0, le=MAX_AVOID_STRENGTH
    )
    #: la direction : "comme celle-ci, mais plus dense". Un dict axe ->
    #: amplitude signee en unites robustes, accepte aussi sous la forme compacte
    #: `object_density:+0.3,sp_p50:-0.5` pour tenir dans une query string.
    steer: dict[str, float] = Field(default_factory=dict)
    weights: Weights = Field(default_factory=Weights)

    @field_validator("steer", mode="before")
    @classmethod
    def _accepte_la_forme_compacte(cls, value: Any) -> Any:
        return parse_steer(value) if isinstance(value, str) else (value or {})

    @property
    def wants_avoid(self) -> bool:
        return bool(self.avoid_checksum or self.avoid_beatmap_id)

    @property
    def applied_steer(self) -> dict[str, float]:
        """
        La direction telle qu'elle sera REELLEMENT appliquee.

        `normalise_steer` jette les amplitudes nulles. La reponse doit renvoyer
        ce qui a ete fait et non ce qui a ete tape, sinon un slider au repos s'y
        relit comme un slider actif -- et une page qui rejoue une recherche
        depuis sa propre reponse remonterait ses curseurs a zero pour rien.
        """
        return normalise_steer(self.steer)

    def search_kwargs(self, avoid: QueryTarget | None = None) -> dict[str, Any]:
        """
        Les arguments de `Searcher.search`.

        Le repoussoir arrive resolu de l'exterieur : le transformer en cible
        demande de lire le corpus, ce qu'un modele pydantic n'a pas a faire.
        """
        return {
            "k": self.k,
            "star_range": self.star_range,
            "length_ratio": (self.length_min, self.length_max),
            "exclude_same_set": self.exclude_same_set,
            "weights": self.weights.model_dump(),
            "max_per_song": self.max_per_song,
            "diverge": self.diverge or None,
            "allow_unranked": self.allow_unranked,
            "group_filter": self.group_filter or None,
            "avoid": avoid,
            "avoid_strength": self.avoid_strength,
            "steer": self.steer or None,
        }


class SimilarRequest(SearchOptions):
    """
    Une cible, trois formes possibles, par cout croissant pour le serveur.

    `features` est la forme recommandee quand le client sait featuriser : c'est
    un dict lu PAR NOM, donc aucune desynchronisation d'ordre n'est possible.
    `vector` est la forme compacte, positionnelle, et exige donc que le client
    annonce `feature_version`.
    """

    checksum: str | None = None
    beatmap_id: int | None = None
    features: dict[str, Any] | None = None
    vector: list[float] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    feature_version: str | None = None
    featurized_with: str | None = None
    mods: str = ""
    rate: float = Field(1.0, gt=0.0, le=4.0)
    variant: str | None = None


class MultiRequest(SearchOptions):
    """
    Plusieurs graines : "voila cinq maps que j'adore, trouve-m'en d'autres".

    Les deux facons de nommer une map coexistent dans la meme requete, parce
    qu'un client les melange forcement : un lien osu! donne un beatmap_id, une
    map lue en jeu donne un checksum.

    Seules des maps du corpus sont acceptees. Featuriser N fichiers pour en
    faire une moyenne serait possible mais couterait N fois le prix d'une
    recherche, et personne ne l'a demande : le cas reel est une liste de maps
    qu'on aime, donc des maps publiees.
    """

    checksums: list[str] = Field(default_factory=list)
    beatmap_ids: list[int] = Field(default_factory=list)
    mods: str = ""
    rate: float = Field(1.0, gt=0.0, le=4.0)
    variant: str | None = None


# --- construction de la reponse -----------------------------------------------

def _query_block(target: QueryTarget, mods: str, in_corpus: bool) -> dict[str, Any]:
    """
    Ce qui decrit la question posee, une graine ou dix.

    `seeds` et `is_centroid` sont la meme forme partout : une recherche a une
    map rend une liste d'une graine plutot que rien, pour qu'un client n'ait pas
    a savoir par quel endpoint il est passe pour lire la reponse. Et
    `is_centroid` compte : quand il est vrai, les phrases de `differences`
    comparent a la moyenne des choix et non a une vraie map -- une interface qui
    l'ignore laisse croire le contraire.
    """
    return {
        "checksum": target.checksum,
        "beatmap_id": target.beatmap_id,
        "label": target.label,
        "stars": round(target.stars, 2),
        "bpm": round(target.bpm, 1),
        "length_s": round(target.length_s, 1),
        "in_corpus": in_corpus,
        "mods": mods,
        "is_centroid": target.is_centroid,
        "n_seeds": len(target.seeds),
        "star_span": [round(v, 2) for v in target.star_span],
        "seeds": [seed.as_dict() for seed in target.seeds],
    }


def _reply(
    *,
    variant: str,
    target: QueryTarget,
    matches: list[Any],
    options: SearchOptions,
    mods: str,
    in_corpus: bool,
    source: str,
    warnings: list[str],
    n_maps: int,
    avoid: QueryTarget | None = None,
) -> dict[str, Any]:
    return {
        "query": _query_block(target, mods, in_corpus),
        "variant": variant,
        "diverge": options.diverge or None,
        "group_filter": options.group_filter or None,
        # la direction telle qu'elle a ete COMPRISE, comme le repoussoir : un
        # client qui a envoye la forme compacte doit pouvoir verifier ce que le
        # serveur en a fait, et une page qui rejoue une recherche la relit ici
        "steer": options.applied_steer or None,
        # le repoussoir est renvoye tel qu'il a ete compris, pas tel qu'il a ete
        # demande : un client qui l'a nomme par beatmap_id doit pouvoir verifier
        # sur quelle map il est reellement tombe
        "avoid": (
            {
                "checksum": avoid.checksum,
                "beatmap_id": avoid.beatmap_id,
                "label": avoid.label,
                "strength": options.avoid_strength,
            }
            if avoid is not None
            else None
        ),
        "count": len(matches),
        "matches": [match.as_dict() for match in matches],
        "source": source,
        "feature_version": FEATURE_VERSION,
        "corpus": {"variant": variant, "n_maps": n_maps},
        "warnings": warnings,
    }


def _target_from_payload(
    searcher: Searcher, payload: SimilarRequest
) -> tuple[QueryTarget, str, list[str]]:
    """
    Construit la cible a partir d'un dict de features ou d'un vecteur.

    Les deux chemins finissent dans `target_from_features`, c'est-a-dire
    exactement le meme code que la map live du serveur local : le scaler du
    serveur s'applique aux valeurs brutes du client, jamais l'inverse.
    """
    warnings: list[str] = []

    if payload.features is not None:
        features = dict(payload.features)
        source = "features"
    else:
        values = payload.vector or []
        if len(values) != len(FEATURE_NAMES):
            raise HTTPException(
                400,
                f"vector has {len(values)} values, expected {len(FEATURE_NAMES)}.",
            )
        # un vecteur est positionnel : sans version annoncee, rien ne dit que
        # les colonnes sont les bonnes, donc on refuse au lieu de deviner
        if not payload.feature_version:
            raise HTTPException(
                400,
                "feature_version is required when posting a raw vector "
                f"(this server serves {FEATURE_VERSION}).",
            )
        features = dict(zip(FEATURE_NAMES, (float(v) for v in values)))
        source = "vector"

    for key, value in (payload.meta or {}).items():
        features.setdefault(key, value)

    if payload.feature_version and payload.feature_version != FEATURE_VERSION:
        raise HTTPException(
            409,
            f"feature set mismatch: client {payload.feature_version!r}, "
            f"server {FEATURE_VERSION!r}. Update the client, or point it at a "
            "server built from the same feature set.",
        )

    stars = features.get("stars")
    if not isinstance(stars, (int, float)) or float(stars) <= 0.0:
        raise HTTPException(
            400,
            "stars is required (it is the difficulty filter, not a feature). "
            "Send it in `features` or in `meta`.",
        )

    try:
        target = searcher.target_from_features(features)
    except KeyError as exc:
        raise HTTPException(400, f"missing feature: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"unusable features: {exc}") from exc

    if payload.featurized_with is not None:
        canonical = "" if payload.variant in (None, "nomod") else payload.variant
        supplied = payload.featurized_with or ""
        if supplied.upper() != (canonical or "").upper():
            warnings.append(
                f"features were computed with mods {supplied or 'none'!r} but the "
                f"corpus was built with {canonical or 'none'!r}; distances are biased"
            )
    return target, source, warnings


# --- routeur ------------------------------------------------------------------

def build_router(service: SearchService) -> APIRouter:
    """
    Le routeur monte a l'identique par le serveur local et par l'API exposee.

    Les differences de deploiement (jeton, quota) vivent dans la config du
    service, pas dans deux jeux d'endpoints : un seul protocole a tester, et un
    client qui bascule d'un hote a l'autre sans changer une ligne.
    """
    config = service.config
    limiter = RateLimiter(config.rate_per_minute, config.burst)

    def client_key(request: Request) -> str:
        if config.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def authorise(request: Request) -> None:
        if not config.token:
            return
        header = request.headers.get("authorization", "")
        supplied = (
            header[7:].strip()
            if header[:7].lower() == "bearer "
            else request.headers.get("x-api-key", "")
        )
        if not secrets.compare_digest(supplied, config.token):
            service.stats["unauthorised"] += 1
            raise HTTPException(401, "Missing or invalid API token.")

    def guard(cost: float = 1.0):
        def dependency(request: Request, _: None = Depends(authorise)) -> None:
            service.stats["requests"] += 1
            wait = limiter.take(client_key(request), cost)
            if wait > 0.0:
                service.stats["throttled"] += 1
                raise HTTPException(
                    429,
                    "Rate limit exceeded. Slow down, or run the tool locally "
                    "(it needs no server at all).",
                    headers={"Retry-After": str(int(wait) + 1)},
                )

        return dependency

    def options_from_query(
        k: int = Query(20, ge=1, le=100),
        star_range: float = Query(DEFAULT_STAR_RANGE, ge=0.0, le=10.0),
        length_min: float = Query(0.5, ge=0.05, le=1.0),
        length_max: float = Query(2.0, ge=1.0, le=20.0),
        exclude_same_set: bool = True,
        max_per_song: int = Query(1, ge=0, le=10),
        diverge: str | None = None,
        allow_unranked: bool = True,
        group_filter: str | None = None,
        avoid_checksum: str | None = None,
        avoid_beatmap_id: int | None = None,
        avoid_strength: float = Query(
            DEFAULT_AVOID_STRENGTH, ge=0.0, le=MAX_AVOID_STRENGTH
        ),
        steer: str | None = Query(
            None,
            description="Directional axes, e.g. object_density:+0.3,sp_p50:-0.5",
        ),
        w_aim: float = Query(1.0, ge=0.0, le=2.0),
        w_speed: float = Query(1.0, ge=0.0, le=2.0),
        w_rhythm: float = Query(1.0, ge=0.0, le=2.0),
        w_spacing: float = Query(1.0, ge=0.0, le=2.0),
        w_parameters: float = Query(1.0, ge=0.0, le=2.0),
    ) -> SearchOptions:
        try:
            return SearchOptions(
                k=k,
                star_range=star_range,
                length_min=length_min,
                length_max=length_max,
                exclude_same_set=exclude_same_set,
                max_per_song=max_per_song,
                diverge=diverge,
                allow_unranked=allow_unranked,
                group_filter=group_filter,
                avoid_checksum=avoid_checksum,
                avoid_beatmap_id=avoid_beatmap_id,
                avoid_strength=avoid_strength,
                steer=steer,
                weights=Weights(
                    aim=w_aim,
                    speed=w_speed,
                    rhythm=w_rhythm,
                    spacing=w_spacing,
                    parameters=w_parameters,
                ),
            )
        except ValidationError as exc:
            # Le modele est construit ICI, dans le corps de la dependance, donc
            # FastAPI ne voit pas passer l'erreur et rendrait un 500 sur un
            # `steer` mal ecrit. Les autres champs sont deja bornes par `Query`
            # et ne peuvent pas arriver jusqu'ici.
            raise HTTPException(400, exc.errors()[0].get("msg", "invalid query"))

    async def read_body(request: Request) -> bytes:
        """Lit le corps en refusant tot ce qui depasse la taille autorisee."""
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "multipart/form-data":
            raise HTTPException(
                415,
                "Send the .osu file as the raw request body "
                "(curl --data-binary @map.osu), not as a multipart form.",
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > config.max_upload:
            raise HTTPException(413, f"File too large (max {config.max_upload} bytes).")

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > config.max_upload:
                raise HTTPException(
                    413, f"File too large (max {config.max_upload} bytes)."
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            raise HTTPException(400, "Empty body: post the .osu file bytes.")
        return raw

    async def resolve_avoid(
        name: str, searcher: Searcher, options: SearchOptions
    ) -> QueryTarget | None:
        """
        Resout le contre-exemple, ou refuse la requete.

        Un repoussoir introuvable est une erreur et pas un avertissement : le
        taire rendrait une liste qui a l'air juste et qui ignore la seule
        contrainte que l'utilisateur a pris la peine d'exprimer. C'est le
        contraire du choix fait pour les graines de /similar/multi, ou en perdre
        une sur cinq laisse une reponse encore utile -- et le dit.
        """
        if not options.wants_avoid:
            return None
        found = None
        if options.avoid_checksum:
            found = await service.target_from_checksum(
                name, searcher, options.avoid_checksum.strip()
            )
        if found is None and options.avoid_beatmap_id:
            found = await service.target_from_beatmap_id(
                name, searcher, options.avoid_beatmap_id
            )
        if found is None:
            raise HTTPException(
                404,
                "The map to avoid is not in this corpus. Only corpus maps can "
                "be used as a counter-example.",
            )
        return found

    router = APIRouter(prefix=API_PREFIX, tags=["osu-mapmatcher"])

    # -- description -----------------------------------------------------------

    @router.get("/meta", summary="Feature set, variants and limits")
    def meta(response: Response) -> dict[str, Any]:
        """
        Ce qu'un client lit au demarrage : version du jeu de features, variantes
        servies, bornes acceptees.

        `no-store` n'est pas une precaution de principe : `supports` est ce qui
        decide si une page montre une fonctionnalite, et une reponse en cache
        est une reponse d'AVANT le dernier deploiement. Vu en vrai -- l'API
        annoncait `steer`, la page l'avait, et le panneau restait cache parce
        que le navigateur relisait un /meta d'il y a dix minutes.
        """
        response.headers["Cache-Control"] = "no-store"
        searchers = service._provider()
        sizes: dict[str, int] = {}
        for name in service.available():
            try:
                sizes[name] = int(searchers.get(name).index.size) if searchers else 0
            except Exception:  # une variante illisible ne doit pas casser /meta
                log.exception("variante %s illisible", name)
        return {
            "feature_version": FEATURE_VERSION,
            "n_features": len(FEATURE_NAMES),
            "feature_names": list(FEATURE_NAMES),
            "groups": list(GROUP_NAMES),
            "variants": service.available(),
            "n_maps": sizes,
            # Ce qu'un client peut demander a CE serveur-ci. L'index hebergé se
            # met a jour par `git pull` : entre le moment ou une page nouvelle
            # est publiee et celui ou le VPS est redeploye, elle tombe sur un
            # serveur qui ignore /similar/multi. Mieux vaut qu'elle le lise ici
            # et masque le bouton que d'envoyer une requete qui rend 404.
            "supports": ["multi", "group_filter", "avoid", "steer"],
            # Le vocabulaire de la recherche directionnelle, servi plutot que
            # recopie dans la page : les memes mots que sous chaque resultat,
            # et une page publiee reste juste quand un axe est ajoute ici.
            "steer_axes": [
                {"axis": axis, "more": more, "less": less}
                for axis, (more, less) in STEER_AXES.items()
            ],
            "limits": {
                "k_max": 100,
                "max_seeds": MAX_SEEDS,
                "avoid_strength_max": MAX_AVOID_STRENGTH,
                "steer_max": MAX_STEER,
                "max_upload_bytes": config.max_upload,
                "rate_per_minute": config.rate_per_minute,
                "file_cost": config.file_cost,
                "auth_required": bool(config.token),
            },
        }

    @router.get("/map/{key}", summary="Is this map in the corpus?")
    async def map_info(
        key: str,
        variant: str | None = None,
        mods: str = "",
        _: None = Depends(guard(1.0)),
    ) -> dict[str, Any]:
        """
        Permet au client de savoir s'il doit envoyer le fichier : une map du
        corpus se cherche par checksum seul, une map inconnue doit etre uploadee.

        `key` accepte aussi un beatmap_id, parce qu'un lien osu! n'en donne pas
        d'autre. Une page qui laisse coller plusieurs liens doit pouvoir montrer
        de quelle map il s'agit AVANT de chercher -- sinon elle affiche une
        liste de numeros, ou elle paie une recherche entiere pour un libelle.
        Un identifiant numerique rendait 404 auparavant : rien ne se casse.
        """
        name, searcher, warnings = service.pick(variant=variant, mods=mods)
        wanted = key.strip()
        target = await service.target_from_checksum(name, searcher, wanted)
        if target is None and wanted.isdigit():
            target = await service.target_from_beatmap_id(name, searcher, int(wanted))
        if target is None:
            raise HTTPException(404, "Unknown checksum or beatmap id in this corpus.")
        return {
            "variant": name,
            "warnings": warnings,
            **_query_block(target, mods, True),
        }

    # -- recherche -------------------------------------------------------------

    @router.get("/similar", summary="Neighbours of a corpus map")
    async def similar_get(
        checksum: str | None = None,
        beatmap_id: int | None = None,
        variant: str | None = None,
        mods: str = "",
        rate: float = Query(1.0, gt=0.0, le=4.0),
        options: SearchOptions = Depends(options_from_query),
        _: None = Depends(guard(1.0)),
    ) -> dict[str, Any]:
        """Forme GET, pour une map deja dans le corpus. Zero CPU cote serveur."""
        name, searcher, warnings = service.pick(variant=variant, mods=mods, rate=rate)
        target = None
        if checksum:
            target = await service.target_from_checksum(name, searcher, checksum.strip())
        elif beatmap_id:
            target = await service.target_from_beatmap_id(name, searcher, beatmap_id)
        else:
            raise HTTPException(400, "Pass either checksum or beatmap_id.")
        if target is None:
            raise HTTPException(
                404,
                "This map is not in the corpus. Post its .osu to "
                f"{API_PREFIX}/similar/file instead.",
            )
        avoid = await resolve_avoid(name, searcher, options)
        matches = await service.search(name, searcher, target, options, avoid)
        return _reply(
            variant=name,
            target=target,
            matches=matches,
            options=options,
            mods=mods,
            in_corpus=True,
            source="corpus",
            warnings=warnings,
            n_maps=int(searcher.index.size),
            avoid=avoid,
        )

    @router.post(
        "/similar", summary="Neighbours of a checksum, a feature dict or a vector"
    )
    async def similar_post(
        payload: SimilarRequest,
        _: None = Depends(guard(1.0)),
    ) -> dict[str, Any]:
        """
        Le chemin normal d'un client qui sait featuriser : il envoie ses 96
        valeurs, le serveur ne fait que la distance.
        """
        name, searcher, warnings = service.pick(
            variant=payload.variant, mods=payload.mods, rate=payload.rate
        )
        source = "corpus"
        target: QueryTarget | None = None

        if payload.checksum:
            target = await service.target_from_checksum(
                name, searcher, payload.checksum.strip()
            )
        if target is None and payload.beatmap_id:
            target = await service.target_from_beatmap_id(
                name, searcher, payload.beatmap_id
            )
        if target is None and (payload.features is not None or payload.vector):
            target, source, extra = _target_from_payload(searcher, payload)
            warnings = warnings + extra
        if target is None:
            raise HTTPException(
                404,
                "Nothing to search with: pass a checksum that exists in the "
                "corpus, a features dict, a vector, or post the .osu to "
                f"{API_PREFIX}/similar/file.",
            )

        in_corpus = source == "corpus" or await service.in_corpus(
            name, searcher, target.checksum
        )
        avoid = await resolve_avoid(name, searcher, payload)
        matches = await service.search(name, searcher, target, payload, avoid)
        return _reply(
            variant=name,
            target=target,
            matches=matches,
            options=payload,
            mods=payload.mods,
            in_corpus=in_corpus,
            source=source,
            warnings=warnings,
            n_maps=int(searcher.index.size),
            avoid=avoid,
        )

    @router.post("/similar/multi", summary="Neighbours of several maps at once")
    async def similar_multi(
        payload: MultiRequest,
        _: None = Depends(guard(1.0)),
    ) -> dict[str, Any]:
        """
        Le centroide de plusieurs graines, et ses voisins.

        La reponse a la meme forme que les autres endpoints ; seul `query`
        change de sens : il decrit l'ensemble des graines, avec la liste dans
        `query.seeds` et `query.is_centroid` a vrai. Les phrases de
        `differences` comparent alors a la MOYENNE des choix, pas a une map.
        """
        name, searcher, warnings = service.pick(
            variant=payload.variant, mods=payload.mods, rate=payload.rate
        )

        wanted = [c.strip() for c in payload.checksums if c and c.strip()]
        wanted_ids = [i for i in payload.beatmap_ids if i]
        if not wanted and not wanted_ids:
            raise HTTPException(400, "Pass at least one checksum or beatmap_id.")
        if len(wanted) + len(wanted_ids) > MAX_SEEDS:
            raise HTTPException(
                400,
                f"Too many seeds ({len(wanted) + len(wanted_ids)}), "
                f"the limit is {MAX_SEEDS}.",
            )

        seeds: list[QueryTarget] = []
        seen: set[str] = set()
        missing: list[str] = []

        async def take(found: QueryTarget | None, asked: str) -> None:
            if found is None:
                missing.append(asked)
                return
            # Deux fois la meme map la ferait peser double dans la moyenne,
            # c'est-a-dire exactement la ponderation qu'on a choisi de ne pas
            # faire. Un doublon vient d'un copier-coller, pas d'une intention.
            if found.checksum in seen:
                warnings.append(f"{asked} was listed twice, counted once")
                return
            seen.add(found.checksum)
            seeds.append(found)

        for checksum in wanted:
            await take(
                await service.target_from_checksum(name, searcher, checksum), checksum
            )
        for beatmap_id in wanted_ids:
            await take(
                await service.target_from_beatmap_id(name, searcher, beatmap_id),
                str(beatmap_id),
            )

        # Une graine perdue sur cinq laisse une reponse qui vaut encore quelque
        # chose, a condition de le dire. Zero graine resolue, non : il n'y a
        # rien a repondre, et un centroide du vide n'existe pas.
        if missing:
            warnings.append(
                f"{len(missing)} seed(s) not in this corpus, ignored: "
                + ", ".join(missing[:5])
                + (" ..." if len(missing) > 5 else "")
            )
        if not seeds:
            raise HTTPException(
                404,
                "None of these maps is in the corpus. Search by checksum or "
                "beatmap_id of a published map.",
            )

        target = combine_targets(seeds)
        low, high = target.star_span
        if high - low > WIDE_SPAN_STARS:
            warnings.append(
                f"seeds span {low:.2f} to {high:.2f} stars; their average is a "
                "compromise that may resemble none of them"
            )

        avoid = await resolve_avoid(name, searcher, payload)
        matches = await service.search(name, searcher, target, payload, avoid)
        return _reply(
            variant=name,
            target=target,
            matches=matches,
            options=payload,
            mods=payload.mods,
            # un centroide n'est aucune map du corpus, meme si toutes ses
            # graines en sont
            in_corpus=False,
            source="corpus",
            warnings=warnings,
            n_maps=int(searcher.index.size),
            avoid=avoid,
        )

    _BINARY_BODY = {
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    }

    @router.post(
        "/similar/file",
        summary="Neighbours of a raw .osu file",
        openapi_extra=_BINARY_BODY,
    )
    async def similar_file(
        request: Request,
        variant: str | None = None,
        mods: str = "",
        rate: float = Query(1.0, gt=0.0, le=4.0),
        options: SearchOptions = Depends(options_from_query),
        _: None = Depends(guard(config.file_cost)),
    ) -> dict[str, Any]:
        """
        Le chemin qui ne demande aucune dependance au client : il envoie le
        fichier, le serveur fait tout. C'est ce qui rend possible un compteur
        tosu en HTML/JS pur, qui ne peut pas heberger de Python.
        """
        raw = await read_body(request)
        name, searcher, warnings = service.pick(variant=variant, mods=mods, rate=rate)

        # le checksum d'une map EST le md5 de son .osu : si elle est dans le
        # corpus, son vecteur est deja calcule et l'upload n'a coute aucun CPU
        checksum = hashlib.md5(raw).hexdigest()
        target = await service.target_from_checksum(name, searcher, checksum)
        source = "corpus"
        if target is None:
            result = await service.featurize(raw, name)
            if result.features is None:
                raise HTTPException(
                    422, f"This file cannot be used: {result.skip_reason}."
                )
            target = searcher.target_from_features(result.features)
            source = "file"

        avoid = await resolve_avoid(name, searcher, options)
        matches = await service.search(name, searcher, target, options, avoid)
        return _reply(
            variant=name,
            target=target,
            matches=matches,
            options=options,
            mods=mods,
            in_corpus=source == "corpus",
            source=source,
            warnings=warnings,
            n_maps=int(searcher.index.size),
            avoid=avoid,
        )

    @router.post(
        "/featurize",
        summary="Feature vector of a raw .osu file, without searching",
        openapi_extra=_BINARY_BODY,
    )
    async def featurize_file(
        request: Request,
        variant: str | None = None,
        mods: str = "",
        _: None = Depends(guard(config.file_cost)),
    ) -> dict[str, Any]:
        """
        Featurise sans chercher : un client peut calculer une fois, garder le
        resultat, et n'envoyer ensuite que le dict a /similar.
        """
        raw = await read_body(request)
        name, _searcher, warnings = service.pick(variant=variant, mods=mods)
        result = await service.featurize(raw, name)
        if result.features is None:
            raise HTTPException(422, f"This file cannot be used: {result.skip_reason}.")
        return {
            "feature_version": FEATURE_VERSION,
            "variant": name,
            "featurized_with": "" if name == "nomod" else name,
            "features": result.features,
            "warnings": warnings,
        }

    # -- exploitation ----------------------------------------------------------

    @router.get("/stats", summary="Counters since startup")
    def stats(_: None = Depends(guard(1.0))) -> dict[str, Any]:
        return {
            "uptime_s": round(time.time() - service.started_at, 1),
            "variants": service.available(),
            "counters": dict(service.stats),
            "cache": {"size": len(service._cache), "max": config.cache_size},
            # travaux CPU simultanes dans CE processus, a ne pas confondre avec
            # le nombre de workers uvicorn
            "concurrency": config.workers,
        }

    return router


# --- application autonome -----------------------------------------------------

def create_api(
    index_dir: Path | None = None,
    config: ApiConfig | None = None,
) -> FastAPI:
    """L'API seule : pas de tosu, pas de page, pas d'etat de session."""
    resolved = config or ApiConfig.from_env()
    root = Path(index_dir) if index_dir else index_root()
    holder: dict[str, SearcherSet | None] = {"searchers": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        searchers = SearcherSet(root)
        found = searchers.available()
        if not found:
            # on ne meurt pas : /health repond ready=false, ce qui laisse un
            # balanceur sortir l'instance du pool au lieu de boucler sur des
            # redemarrages, et le message reste lisible dans les logs
            log.error(
                "aucun index dans %s : l'API repondra 503. "
                "Recupere un index (scripts/fetch_index.py) ou construis-le.",
                root,
            )
        else:
            log.info("index servis : %s", ", ".join(found))
            searchers.get("nomod" if "nomod" in found else found[0])  # prechauffage
        holder["searchers"] = searchers
        try:
            yield
        finally:
            searchers.close()
            holder["searchers"] = None

    app = FastAPI(
        title="osu!MapMatcher API",
        version="1.0",
        summary="Structural beatmap neighbours, over HTTP.",
        lifespan=lifespan,
        docs_url="/docs" if resolved.docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.docs else None,
    )

    service = SearchService(lambda: holder["searchers"], resolved)
    app.state.service = service

    if resolved.cors_origins or resolved.cors_regex:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_origin_regex=resolved.cors_regex or None,
            allow_credentials=False,  # une API publique n'a pas de session
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-API-Key"],
            max_age=86400,
        )

    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.include_router(build_router(service))

    @app.get("/", include_in_schema=False)
    def root_page() -> dict[str, Any]:
        return {
            "name": "osu!MapMatcher API",
            "feature_version": FEATURE_VERSION,
            "variants": service.available(),
            "meta": f"{API_PREFIX}/meta",
            "explore": "/explore",
            "docs": "/docs" if resolved.docs else None,
            "source": "https://github.com/Ayreth/OsuMapMatcher",
        }

    @app.get("/explore", include_in_schema=False)
    def explore_page() -> FileResponse:
        """
        La page publique, servie par l'API elle-meme.

        Un fichier unique, sans aucune ressource externe : pas de dossier
        statique a monter ici, et rien a servir de plus. C'est ce qui permet a
        quelqu'un qui n'installe rien du tout d'utiliser l'outil -- ce pour quoi
        cette API existe. Le meme fichier est servi par le serveur local, ou il
        interroge l'index de la machine.
        """
        return FileResponse(STATIC_DIR / "explore.html")

    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "ready": service.ready(),
            "variants": service.available(),
            "feature_version": FEATURE_VERSION,
            "uptime_s": round(time.time() - service.started_at, 1),
        }

    return app


#: cible d'un `uvicorn osumapmatcher.api:app --workers 4`
app = create_api()


# --- point d'entree -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="osu!MapMatcher hosted API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=1,
                        help="processus uvicorn (chacun a son quota et son cache)")
    parser.add_argument("--fetch", action="store_true",
                        help="telecharge les variantes manquantes avant de demarrer")
    parser.add_argument("--variants", default="",
                        help="variantes a servir, ex. nomod,DT (defaut : toutes)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.variants:
        os.environ["OSUMAPMATCHER_API_VARIANTS"] = args.variants
    if args.fetch:
        from .launcher import ensure_index

        wanted = [v.strip() for v in (args.variants or "nomod").split(",") if v.strip()]
        if not ensure_index(wanted):
            return 1

    config = ApiConfig.from_env()
    print(f"osu!MapMatcher API on http://{args.host}:{args.port}")
    print(f"  index          : {args.index or index_root()}")
    print(f"  feature version: {FEATURE_VERSION}")
    print(f"  rate limit     : {config.rate_per_minute:g}/min per IP"
          f"{' per worker' if args.workers > 1 else ''}")
    print(f"  auth           : {'bearer token' if config.token else 'open'}")

    if args.workers > 1:
        # uvicorn --workers a besoin d'une chaine d'import : la config repasse
        # alors par l'environnement, pas par les arguments
        if args.index:
            os.environ["OSUMAPMATCHER_DATA"] = str(Path(args.index).parent)
        uvicorn.run(
            "osumapmatcher.api:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level="warning",
        )
    else:
        uvicorn.run(
            create_api(args.index),
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
