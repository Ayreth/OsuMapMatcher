#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote.py - client de l'API hebergee, pour le mode "serveur" du serveur local.

Le seul interet du mode distant est de supprimer le telechargement de l'index :
un joueur qui ne veut rien installer garde tosu et cette page, et laisse le
calcul a une machine qui a deja le corpus. Tout le reste est identique -- meme
protocole, memes reponses -- parce que l'API distante monte exactement le meme
routeur que le serveur local (voir api.py).

Trois facons de poser la question, essayees dans cet ordre :

  1. le checksum de la map. C'est le cas normal : toute map ranked ou loved est
     deja dans le corpus du serveur, qui a donc deja son vecteur. 32 octets sur
     le fil, zero calcul des deux cotes.
  2. les features calculees ici, si le serveur annonce le meme jeu de features.
     Sert aux maps absentes du corpus (non soumises, editees) : ~3 Ko de JSON,
     et le fichier du joueur ne quitte pas sa machine.
  3. le .osu brut, quand les versions de features different. Le serveur
     featurise avec SON code, donc le resultat reste juste meme si le client
     est plus vieux que le serveur -- c'est ce qui evite de casser tout le monde
     a chaque mise a jour du corpus.

Aucune dependance ajoutee : urllib suffit, et le point de tout ceci est
justement une installation legere.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .features import FEATURE_VERSION

log = logging.getLogger(__name__)

#: instance interrogee par defaut. `OSURECO_API_URL` la remplace.
DEFAULT_API_URL = "https://api.ayreth.site"

#: Obligatoire. Derriere Cloudflare, un client qui garde le User-Agent par
#: defaut de Python se fait renvoyer 403 par la protection anti-bot (mesure :
#: `Python-urllib/3.12` refuse systematiquement, tout UA explicite passe).
USER_AGENT = "osureco/0.1 (+https://github.com/Ayreth/OsuMapMatcher)"

DEFAULT_TIMEOUT = 10.0
#: duree de validite de /meta : le corpus d'un serveur ne bouge pas souvent
META_TTL_S = 300.0
#: apres un echec, on laisse le serveur tranquille pendant ce delai
COOLDOWN_S = 20.0


class RemoteError(RuntimeError):
    """Echec cote distant, avec de quoi decider s'il faut basculer en local."""

    def __init__(self, message: str, *, status: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail

    @property
    def transient(self) -> bool:
        """Vrai si le probleme vient du serveur ou du reseau, pas de la requete."""
        return self.status == 0 or self.status in (429, 500, 502, 503, 504)

    def as_text(self) -> str:
        """Version complete, pour les logs."""
        if self.status:
            return f"{self.status} {self.detail or self}".strip()
        return str(self)

    def summary(self) -> str:
        """Version courte, pour l'en-tete de la page."""
        if self.status == 0:
            return "unreachable"
        if self.status == 403:
            return "blocked by the edge (403)"
        if self.status == 409:
            return "feature set mismatch"
        if self.status == 429:
            return "rate limited"
        if self.status >= 500:
            return f"server error {self.status}"
        return f"error {self.status}"


@dataclass
class RemoteInfo:
    """Ce que le serveur dit de lui-meme."""

    feature_version: str = ""
    variants: list[str] = field(default_factory=list)
    n_maps: dict[str, int] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def compatible(self) -> bool:
        """Le serveur accepte-t-il des features calculees ici ?"""
        return self.feature_version == FEATURE_VERSION


class RemoteBackend:
    """
    Client HTTP synchrone de l'API. A appeler depuis un thread : le serveur
    local le fait via `asyncio.to_thread`, pour ne pas bloquer le SSE.
    """

    def __init__(self, base_url: str = DEFAULT_API_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_error: str = ""
        self._info: RemoteInfo | None = None
        self._blocked_until = 0.0

    # -- transport -------------------------------------------------------------

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            pairs = [
                (key, "true" if value is True else "false" if value is False else str(value))
                for key, value in params.items()
                if value is not None and value != ""
            ]
            url += "?" + urllib.parse.urlencode(pairs)
        return url

    def _call(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str = "",
        timeout: float | None = None,
    ) -> Any:
        request = urllib.request.Request(
            self._url(path, params),
            data=body,
            method="POST" if body is not None else "GET",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(payload).get("detail", "")
            except (json.JSONDecodeError, AttributeError):
                # 403 de Cloudflare, page d'erreur d'un proxy : ce n'est pas
                # l'API qui repond, donc pas de champ `detail`
                detail = payload.strip()[:200]
            raise RemoteError(
                f"the server answered {exc.code}", status=exc.code, detail=detail
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteError(f"{type(exc).__name__}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RemoteError(f"unreadable answer: {exc}") from exc

    # -- etat ------------------------------------------------------------------

    def info(self, *, force: bool = False) -> RemoteInfo:
        """Description du serveur, gardee en cache : elle ne bouge presque pas."""
        fresh = self._info and (time.monotonic() - self._info.fetched_at) < META_TTL_S
        if fresh and not force:
            return self._info  # type: ignore[return-value]

        payload = self._call("/api/v1/meta")
        self._info = RemoteInfo(
            feature_version=str(payload.get("feature_version", "")),
            variants=list(payload.get("variants", [])),
            n_maps=dict(payload.get("n_maps", {})),
            fetched_at=time.monotonic(),
        )
        return self._info

    def probe(self) -> bool:
        """Le serveur repond-il ? Memorise l'erreur, ne leve pas."""
        try:
            self.info(force=True)
        except RemoteError as exc:
            log.warning("api %s injoignable : %s", self.base_url, exc.as_text())
            self.last_error = exc.summary()
            self._blocked_until = time.monotonic() + COOLDOWN_S
            return False
        self.last_error = ""
        self._blocked_until = 0.0
        return True

    @property
    def cooling_down(self) -> bool:
        """Vrai juste apres un echec : inutile de re-tenter a chaque map."""
        return time.monotonic() < self._blocked_until

    # -- recherche -------------------------------------------------------------

    def search(
        self,
        *,
        checksum: str = "",
        mods: str = "",
        rate: float = 1.0,
        options: dict[str, Any] | None = None,
        features: dict[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> dict[str, Any]:
        """
        Renvoie la reponse de l'API, dans la meme forme que la recherche locale.

        L'ordre des tentatives est celui du cout : checksum, puis features, puis
        fichier. On ne monte d'un cran que si le cran precedent ne peut pas
        repondre, jamais parce qu'il a echoue pour une autre raison.
        """
        query = dict(options or {})
        query.update(mods=mods, rate=rate)

        try:
            if checksum:
                payload = self._call("/api/v1/similar", params={"checksum": checksum, **query})
                self.last_error = ""
                self._blocked_until = 0.0
                return payload
        except RemoteError as exc:
            if exc.status != 404:
                self._remember(exc)
                raise
            # 404 = map absente du corpus distant, c'est un cas normal :
            # on continue avec ce qu'on sait calculer ici

        try:
            payload = self._fallback(query, features, raw)
        except RemoteError as exc:
            self._remember(exc)
            raise
        self.last_error = ""
        self._blocked_until = 0.0
        return payload

    def _fallback(
        self, query: dict[str, Any], features: dict[str, Any] | None, raw: bytes | None
    ) -> dict[str, Any]:
        compatible = False
        try:
            compatible = self.info().compatible
        except RemoteError:
            pass  # on tentera le fichier, qui ne depend d'aucune version

        if features and compatible:
            body = json.dumps({
                "features": features,
                "feature_version": FEATURE_VERSION,
                "featurized_with": _canonical_mods(query.get("mods", "")),
                **query,
            }).encode("utf-8")
            return self._call(
                "/api/v1/similar", body=body, content_type="application/json"
            )

        if raw:
            # Le serveur featurise avec son propre code : c'est la seule voie
            # qui reste juste quand client et serveur n'ont pas le meme jeu de
            # features.
            return self._call(
                "/api/v1/similar/file",
                params=query,
                body=raw,
                content_type="application/octet-stream",
            )

        raise RemoteError(
            "this map is not in the server's corpus, and nothing local could be "
            "sent instead"
        )

    def _remember(self, exc: RemoteError) -> None:
        log.warning("api %s : %s", self.base_url, exc.as_text())
        self.last_error = exc.summary()
        if exc.transient:
            self._blocked_until = time.monotonic() + COOLDOWN_S


def _canonical_mods(mods: str) -> str:
    """Les mods avec lesquels la featurisation locale a ete faite."""
    from .features import variant_for_mods

    variant = variant_for_mods(mods)
    return "" if variant == "nomod" else variant
