#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unranked.py - recuperation des maps non classees les plus jouees.

Le dump ppy ne contient que du ranked et du loved. Les maps graveyard, meme
celles qui totalisent trente millions de plays, n'y sont pas : elles peuvent
servir de requete (le serveur featurise en live ce qu'il ne connait pas) mais
ne peuvent jamais etre recommandees. Ce module va les chercher ailleurs.

Deux besoins distincts, et une seule source publique les couvre tous les deux :

  * le CLASSEMENT par nombre de plays. L'API officielle sait le faire
    (`/beatmapsets/search?sort=plays_desc`) mais exige un jeton OAuth ;
    osu.direct le fait sans authentification. Les mirrors au format Cheesegull
    (hinamizawa) n'exposent pas de playcount et ne peuvent donc pas classer.
  * les fichiers .osu eux-memes, que tout le monde sait servir.

Point verifie avant d'ecrire une ligne de ce fichier : pour une meme map,
osu.ppy.sh, catboy.best et osu.direct renvoient des octets IDENTIQUES -- meme
taille, meme MD5. C'est la condition non negociable, parce que tout l'index est
cle sur le checksum, qui EST le md5 du .osu. Un mirror qui repackagerait, ne
serait-ce qu'en normalisant les fins de ligne, fabriquerait des entrees que le
jeu ne pourrait jamais reconnaitre. Le telechargement revalide donc chaque
fichier contre le md5 annonce par la recherche, et jette ce qui ne colle pas.

Ce module est un outil de BUILD, pas de runtime : si le mirror disparait,
l'index deja construit continue de tourner et l'API hebergee ne bronche pas. On
perd seulement la capacite a rafraichir le pool. C'est ce qui rend la
dependance a un tiers acceptable ici, alors qu'elle ne le serait pas en ligne.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: identifiant envoye a chaque requete. Un mirror qui voit passer 3 700 appels
#: a le droit de savoir qui l'appelle et ou se plaindre.
USER_AGENT = "OsuMapMatcher/1.0 (+https://github.com/Ayreth/OsuMapMatcher)"

#: codes de statut osu!, tels que les attendent les API de recherche
STATUS_CODES: dict[str, int] = {
    "graveyard": -2,
    "wip": -1,
    "pending": 0,
    "qualified": 3,
}

#: loved. Pas dans STATUS_CODES : ces maps-la sont deja dans le dump, on ne va
#: pas les telecharger, seulement recuperer leurs md5 pour les distinguer des
#: ranked -- distinction que le dump ne fait pas.
LOVED_CODE = 4

#: ou atterrit la liste des md5 loved, a cote des index
LOVED_FILE = "loved.json"

#: statuts qui valent la peine par defaut. Le graveyard represente l'immense
#: majorite du volume ; `pending` rattrape les maps encore vivantes.
DEFAULT_STATUSES: tuple[str, ...] = ("graveyard", "pending")

#: osu!standard. Les autres modes sont de toute facon jetes a la featurisation,
#: mais les filtrer ici evite de telecharger pour rien.
MODE_STD = 0

#: page maximale acceptee par osu.direct. Verifie : 100 passent, `offset` suit.
PAGE_SIZE = 100

#: pause entre deux requetes, en secondes. Mesure sur un lot reel de 183 maps :
#: a 0.2 s (5 req/s) osu.direct laisse tomber 4 % des requetes, alors que les
#: memes maps repondent parfaitement en isole -- c'est du throttling, pas des
#: 404. A 0.35 s le lot passe entier. On reste tres au-dessus du 1 req/s que
#: ppy recommande pour sa propre API.
DEFAULT_DELAY = 0.35

#: pause entre deux PAGES de recherche. La recherche et les fichiers ne sont pas
#: limites au meme rythme : a 0.35 s les fichiers passent sans broncher, mais la
#: recherche renvoie des 429 des la quarantieme page. Une page pese ~500 ko et
#: couvre 100 sets, donc rien ne presse -- balayer 36 000 sets ne fait que 361
#: requetes, six minutes a ce rythme.
SEARCH_DELAY = 1.0

#: garde-fou de la pagination quand on descend par seuil de plays plutot que par
#: nombre de sets : sans borne, un mirror qui repondrait toujours quelque chose
#: ferait tourner la boucle indefiniment. 200 000 sets est tres au-dela de ce que
#: le catalogue std contient au-dessus de quelques centaines de plays.
MAX_SEARCH_OFFSET = 200_000

#: une requete sur 3 700 finit quand meme par tomber pour rien. On reessaie
#: avec un recul croissant plutot que de perdre la map.
MAX_ATTEMPTS = 4
RETRY_BACKOFF = 2.0

#: un 429 ne se resout pas en deux secondes : la fenetre du limiteur doit
#: passer. Mesure : les stalls observes duraient ~5 s.
THROTTLE_WAIT = 6.0

#: multiplicateur de la pause pendant la passe de rattrapage. Le lot principal
#: est justement ce qui a declenche le throttling ; le repasser au meme rythme
#: reproduirait l'echec.
RETRY_PASS_SLOWDOWN = 4.0

MANIFEST_FILE = "manifest.json"


# --- sources ------------------------------------------------------------------

@dataclass(frozen=True)
class Mirror:
    """
    Une source de donnees. `search_url` est optionnel : un mirror peut savoir
    servir les fichiers sans savoir les classer, et c'est le cas de la plupart.
    """
    name: str
    osu_url: str
    search_url: str | None = None

    @property
    def can_rank(self) -> bool:
        return self.search_url is not None

    def file_url(self, beatmap_id: int) -> str:
        return self.osu_url.format(beatmap_id=beatmap_id)


MIRRORS: dict[str, Mirror] = {
    # la seule source testee qui sache trier du graveyard par plays sans OAuth
    "osu.direct": Mirror(
        name="osu.direct",
        osu_url="https://osu.direct/api/osu/{beatmap_id}",
        search_url=(
            "https://osu.direct/api/v2/search"
            "?mode={mode}&status={status}&sort=play_count:desc"
            "&amount={amount}&offset={offset}"
        ),
    ),
    # recherche au format Cheesegull : pas de playcount, donc fichiers seulement
    "hinamizawa": Mirror(
        name="hinamizawa",
        osu_url="https://mirror.hinamizawa.ai/api/osu/{beatmap_id}",
    ),
    "catboy": Mirror(
        name="catboy",
        osu_url="https://catboy.best/osu/{beatmap_id}",
    ),
    # la source d'origine, en dernier recours : elle marche sans jeton pour les
    # .osu bruts, mais c'est elle qu'on cherche justement a ne pas marteler
    "ppy": Mirror(
        name="ppy",
        osu_url="https://osu.ppy.sh/osu/{beatmap_id}",
    ),
}

DEFAULT_MIRROR = "osu.direct"

#: mirrors sollicites en parallele pour les fichiers, un thread chacun.
#:
#: Mesure sur 8 maps, md5 verifies contre le manifeste : les quatre sources
#: renvoient des octets identiques, a 148 ms (osu.direct), 156 ms (catboy),
#: 220 ms (hinamizawa) et 469 ms (ppy) par requete.
#:
#: ppy est volontairement hors du pool par defaut : c'est l'origine, la plus
#: lente, et precisement celle qu'on cherche a soulager. Elle reste disponible
#: via `--mirrors` pour qui veut s'en servir.
DEFAULT_POOL: tuple[str, ...] = ("osu.direct", "catboy", "hinamizawa")


# --- ce qu'on recupere --------------------------------------------------------

@dataclass(frozen=True)
class BeatmapRef:
    """Une difficulte std a telecharger, avec de quoi la verifier."""
    beatmap_id: int
    beatmapset_id: int
    checksum: str
    status: str
    playcount: int
    stars: float
    label: str

    @property
    def filename(self) -> str:
        # le dump ppy nomme ses fichiers `{beatmap_id}.osu`, et build_corpus
        # sait recuperer l'identifiant depuis le nom quand les metadonnees
        # portent `BeatmapID:0`. On reproduit exactement cette convention pour
        # que la featurisation marche sans une ligne de code en plus.
        return f"{self.beatmap_id}.osu"


@dataclass
class FetchReport:
    n_sets: int = 0
    n_refs: int = 0
    n_downloaded: int = 0
    n_reused: int = 0
    n_checksum_mismatch: int = 0
    n_failed: int = 0
    failures: list[int] = field(default_factory=list)
    bytes_written: int = 0
    elapsed_s: float = 0.0
    mirrors: list[str] = field(default_factory=list)

    def render(self) -> str:
        debit = self.n_downloaded / self.elapsed_s if self.elapsed_s > 0 else 0.0
        lines = [
            "=" * 70,
            "RAPPORT DE RECUPERATION",
            "=" * 70,
            f"  sets classes      : {self.n_sets}",
            f"  difficultes std   : {self.n_refs}",
            f"  telechargees      : {self.n_downloaded}",
            f"  deja presentes    : {self.n_reused}",
            f"  md5 discordants   : {self.n_checksum_mismatch}",
            f"  echecs reseau     : {self.n_failed}",
            f"  octets ecrits     : {self.bytes_written / 1e6:.1f} Mo",
            f"  mirrors           : {', '.join(self.mirrors) or '-'}",
            f"  duree             : {self.elapsed_s:.0f} s  ({debit:.1f} maps/s)",
        ]
        if self.failures:
            shown = ", ".join(str(i) for i in self.failures[:10])
            more = "" if len(self.failures) <= 10 else f" (+{len(self.failures) - 10})"
            lines.append(f"  ids ecartes       : {shown}{more}")
        lines.append("=" * 70)
        return "\n".join(lines)


# --- http ---------------------------------------------------------------------

def _get(url: str, *, timeout: float = 30.0) -> bytes:
    """
    GET avec reessais. Releve la derniere erreur si tout echoue.

    Un 429 est traite a part : sur un lot de plusieurs milliers de fichiers,
    c'est l'echec le plus frequent et le seul qui se resout tout seul, a
    condition d'attendre reellement que la fenetre du limiteur passe. Un recul
    de deux secondes ne suffit pas et fait perdre la map pour rien.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # 404 : la map n'existe plus chez ce mirror, insister ne sert a rien
            if exc.code == 404:
                raise
            last = exc
            if exc.code == 429 and attempt < MAX_ATTEMPTS - 1:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    pause = float(retry_after) if retry_after else THROTTLE_WAIT
                except (TypeError, ValueError):
                    pause = THROTTLE_WAIT
                time.sleep(min(pause, 60.0))
                continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF ** attempt)
    raise last if last else RuntimeError(f"echec sans exception sur {url}")


# --- classement ---------------------------------------------------------------

def _unwrap(payload: Any) -> list[dict]:
    """osu.direct renvoie une liste nue ; d'autres emballent dans `data`."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "beatmapsets", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _is_std(beatmap: dict) -> bool:
    mode = beatmap.get("mode_int", beatmap.get("mode"))
    return mode in (MODE_STD, "osu")


def list_top_sets(
    n_sets: int | None = None,
    *,
    min_playcount: int = 0,
    statuses: Sequence[str] = DEFAULT_STATUSES,
    mirror: Mirror | None = None,
    delay: float = SEARCH_DELAY,
    progress: bool = True,
) -> list[dict]:
    """
    Les beatmapsets non classes les plus joues, tous statuts confondus.

    Deux facons de dire ou s'arreter, cumulables :

      * `n_sets` -- les n premiers, quel que soit leur playcount. C'est un top.
      * `min_playcount` -- tous ceux qui depassent ce seuil, quel qu'en soit le
        nombre. C'est un pool. « Tout ce qui a plus de 5 000 plays » se decrit
        ainsi et pas autrement : on ne sait pas d'avance combien ca fait.

    Le seuil peut piloter la pagination parce que le tri est decroissant : des
    qu'une page finit sous le seuil, tout ce qui suit y est aussi. Pas besoin de
    lire le catalogue entier pour en extraire une tranche.

    Chaque statut se pagine separement -- l'API ne sait pas trier une union --
    puis on refusionne sur le playcount avant de couper. Demander n sets par
    statut et garder les n meilleurs du melange est correct : un set absent du
    top n de son propre statut ne peut pas etre dans le top n global.
    """
    if n_sets is None and min_playcount <= 0:
        raise ValueError(
            "il faut au moins un critere d'arret : n_sets, min_playcount, ou les deux."
        )
    mirror = mirror or MIRRORS[DEFAULT_MIRROR]
    if not mirror.can_rank:
        raise ValueError(
            f"{mirror.name} ne sait pas classer par plays : sa recherche n'expose "
            "pas de playcount. Utilise --mirror osu.direct pour le classement."
        )

    merged: dict[int, dict] = {}
    for status in statuses:
        if status not in STATUS_CODES:
            raise KeyError(
                f"statut inconnu : {status} "
                f"(connus : {', '.join(sorted(STATUS_CODES))})"
            )
        code = STATUS_CODES[status]
        offset = 0
        while offset < MAX_SEARCH_OFFSET:
            amount = PAGE_SIZE if n_sets is None else min(PAGE_SIZE, n_sets - offset)
            if amount <= 0:
                break
            url = mirror.search_url.format(
                mode=MODE_STD, status=code, amount=amount, offset=offset,
            )
            page = _unwrap(json.loads(_get(url).decode("utf-8")))
            plancher = min((int(e.get("play_count") or 0) for e in page), default=0)
            if progress:
                print(f"  {status:<10} offset {offset:>6} -> {len(page):>3} sets"
                      f"   plancher {plancher:>10,} plays", flush=True)
            if not page:
                break
            for entry in page:
                identifier = entry.get("id")
                if identifier is not None:
                    merged[int(identifier)] = entry
            if len(page) < amount:
                break
            # tri decroissant : une page dont le dernier set est deja sous le
            # seuil est la derniere qui nous interesse
            if min_playcount and plancher < min_playcount:
                break
            offset += len(page)
            time.sleep(delay)
        else:
            if progress:
                print(f"  {status:<10} arret au garde-fou de "
                      f"{MAX_SEARCH_OFFSET:,} sets", flush=True)

    ordered = sorted(
        merged.values(), key=lambda s: int(s.get("play_count") or 0), reverse=True
    )
    if min_playcount:
        ordered = [s for s in ordered if int(s.get("play_count") or 0) >= min_playcount]
    return ordered if n_sets is None else ordered[:n_sets]


def list_loved_checksums(
    *,
    mirror: Mirror | None = None,
    delay: float = DEFAULT_DELAY,
    progress: bool = True,
) -> set[str]:
    """
    Les md5 de toutes les difficultes std **loved**.

    Sert a separer `ranked` de `loved` parmi les maps du dump, qui ne le dit
    pas. Rien n'est telecharge : ces maps sont deja dans le dump, on ne cherche
    que leurs checksums pour etiqueter des lignes existantes. Environ 1 300
    sets, soit quatorze requetes.
    """
    mirror = mirror or MIRRORS[DEFAULT_MIRROR]
    if not mirror.can_rank:
        raise ValueError(f"{mirror.name} n'a pas de recherche utilisable ici.")

    checksums: set[str] = set()
    offset = 0
    while True:
        url = mirror.search_url.format(
            mode=MODE_STD, status=LOVED_CODE, amount=PAGE_SIZE, offset=offset
        )
        page = _unwrap(json.loads(_get(url).decode("utf-8")))
        if not page:
            break
        for entry in page:
            for beatmap in entry.get("beatmaps") or []:
                if _is_std(beatmap) and beatmap.get("checksum"):
                    checksums.add(str(beatmap["checksum"]).lower())
        if progress:
            print(f"  loved  offset {offset:>5} -> {len(page):>3} sets, "
                  f"{len(checksums)} md5", flush=True)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(delay)
    return checksums


def refs_from_sets(
    sets: Iterable[dict],
    *,
    min_stars: float = 0.0,
    max_stars: float = float("inf"),
    max_diffs: int | None = None,
    min_playcount: int = 0,
) -> list[BeatmapRef]:
    """
    Aplatit les sets en difficultes std uniques, sans les autres modes.

    Les filtres portent sur ce que la recherche a DEJA renvoye : etoiles, nombre
    de difficultes et playcount sont tous dans la reponse. Rien n'est donc
    telecharge pour etre jete apres coup -- une difficulte ecartee ici ne coute
    pas une seule requete de fichier, seulement l'octet de JSON qui la decrivait.

    `max_diffs` compte les difficultes **std** du set, pas le total, et la borne
    est inclusive : `max_diffs=10` garde les sets jusqu'a dix difficultes std. Un
    set de trois std et cinquante mania reste un set de trois std. Ce filtre vise
    les paquets d'entrainement a cent difficultes, qui remplissent le pool de
    variations d'une seule et meme chose.

    Les etoiles sont celles **sans mods**, telles que le mirror les annonce. Un
    corpus DT bati sur ce pool verra ces memes maps autour de 1,4x ces valeurs :
    viser une fenetre affichee en DT demande donc d'abaisser `min_stars`
    d'autant, sinon la variante DT n'a rien a recommander en bas de sa fenetre.
    """
    refs: dict[int, BeatmapRef] = {}
    for entry in sets:
        playcount = int(entry.get("play_count") or 0)
        if playcount < min_playcount:
            continue
        std = [b for b in (entry.get("beatmaps") or []) if _is_std(b)]
        if max_diffs is not None and len(std) > max_diffs:
            continue
        set_status = str(entry.get("status") or "unknown")
        artist = str(entry.get("artist") or "")
        title = str(entry.get("title") or "")
        for beatmap in std:
            stars = float(beatmap.get("difficulty_rating") or 0.0)
            if not min_stars <= stars <= max_stars:
                continue
            beatmap_id = beatmap.get("id")
            checksum = beatmap.get("checksum") or ""
            if not beatmap_id or not checksum:
                # sans md5 annonce, on ne pourrait pas verifier le telechargement
                continue
            beatmap_id = int(beatmap_id)
            version = beatmap.get("version") or ""
            refs[beatmap_id] = BeatmapRef(
                beatmap_id=beatmap_id,
                beatmapset_id=int(entry.get("id") or 0),
                checksum=str(checksum).lower(),
                # le statut d'une difficulte peut differer de celui de son set
                status=str(beatmap.get("status") or set_status),
                playcount=playcount,
                stars=stars,
                label=f"{artist} - {title} [{version}]",
            )
    return sorted(refs.values(), key=lambda r: (-r.playcount, r.beatmap_id))


# --- telechargement -----------------------------------------------------------

def _md5(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()

#: issues possibles pour une map, dans l'ordre ou on les rencontre
_REUSED, _OK, _MISMATCH, _FAILED = "reused", "ok", "mismatch", "failed"


def resolve_mirrors(
    spec: "Mirror | Sequence[Mirror] | Sequence[str] | None",
) -> list[Mirror]:
    """
    Normalise ce qu'on nous donne en liste de Mirror, sans doublon.

    Accepte un Mirror seul, une liste de Mirror, une liste de noms, ou None
    pour le pool par defaut.
    """
    if spec is None:
        return [MIRRORS[name] for name in DEFAULT_POOL]
    if isinstance(spec, Mirror):
        return [spec]
    resolus: list[Mirror] = []
    for item in spec:
        mirror = MIRRORS[item] if isinstance(item, str) else item
        if mirror not in resolus:
            resolus.append(mirror)
    if not resolus:
        raise ValueError("aucun mirror utilisable")
    return resolus


def _fetch_one(
    ref: BeatmapRef, out_dir: Path, mirror: Mirror, *, verify: bool, resume: bool
) -> tuple[str, int]:
    """Traite une map depuis un mirror donne. Renvoie (issue, octets ecrits)."""
    destination = out_dir / ref.filename
    if resume and destination.is_file():
        existing = destination.read_bytes()
        if not verify or _md5(existing) == ref.checksum:
            return _REUSED, 0

    try:
        payload = _get(mirror.file_url(ref.beatmap_id))
    except Exception:
        return _FAILED, 0

    if verify and _md5(payload) != ref.checksum:
        # ce mirror sert autre chose que ce qui est annonce : une entree pareille
        # serait introuvable depuis le jeu. Un autre mirror peut tres bien avoir
        # le bon fichier, donc c'est un echec rattrapable ailleurs, pas un rejet.
        return _MISMATCH, 0

    # `.tmp` puis remplacement : un fichier visible est un fichier complet, meme
    # si le job est coupe au mauvais moment. Le nom depend du beatmap_id, donc
    # deux threads ne peuvent pas viser le meme.
    temporary = destination.with_suffix(".osu.tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return _OK, len(payload)


def download_refs(
    refs: Sequence[BeatmapRef],
    out_dir: Path,
    *,
    mirrors: "Mirror | Sequence[Mirror] | Sequence[str] | None" = None,
    delay: float = DEFAULT_DELAY,
    verify: bool = True,
    resume: bool = True,
    progress: bool = True,
    retry_pass: bool = True,
) -> FetchReport:
    """
    Ecrit un `{beatmap_id}.osu` par difficulte, en verifiant chaque md5.

    Un thread par mirror, chacun a son propre rythme. Ce n'est pas qu'une
    optimisation : a 3 req/s sur trois mirrors, chacun voit MOINS de trafic
    qu'avec une boucle unique a 3 req/s sur un seul, pendant que le debit
    cumule triple. Repartir la charge est plus correct vis-a-vis de services
    communautaires gratuits, pas seulement plus rapide.

    L'equilibrage se fait tout seul : les threads tirent dans la meme file, donc
    un mirror rapide sert plus de maps qu'un mirror lent sans qu'on ait a
    mesurer quoi que ce soit.

    Reprise : un fichier deja present dont le md5 colle n'est pas retelecharge.
    Repartir apres une coupure ne coute donc que le hachage de ce qui est la.

    Passe de rattrapage : ce qui echoue est rejoue sur un AUTRE mirror. Sur le
    lot reel de 3 775 maps, 159 s'etaient perdues faute de cette bascule -- un
    mirror qui n'a pas un fichier, ou qui limite a cet instant, ne dit rien des
    trois autres.
    """
    pool = resolve_mirrors(mirrors)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = FetchReport(n_refs=len(refs), mirrors=[m.name for m in pool])
    started = time.perf_counter()

    bar = None
    if progress:
        try:
            from tqdm import tqdm
            # `disable=None` : tqdm se tait tout seul hors terminal, sinon
            # chaque rafraichissement part dans les logs sur sa propre ligne
            bar = tqdm(total=len(refs), unit="map", smoothing=0.05, disable=None)
        except ImportError:
            bar = None

    verrou = threading.Lock()
    par_id = {ref.beatmap_id: ref for ref in refs}

    def _consommer(mirror: Mirror, tirer, pause: float, retry: bool,
                   retombees: dict[int, Mirror]) -> None:
        """Corps d'un worker : il tire des maps et les traite jusqu'a epuisement."""
        while True:
            ref = tirer()
            if ref is None:
                return
            issue, ecrits = _fetch_one(
                ref, out_dir, mirror, verify=verify, resume=resume
            )
            # un echec reseau est transitoire, il vaut un second essai n'importe
            # ou. Un md5 discordant, lui, ne vaut un second essai que s'il
            # existe un AUTRE mirror : le rejouer sur celui qui vient de servir
            # les mauvais octets redonnerait exactement les memes.
            rejouable = issue == _FAILED or (issue == _MISMATCH and len(pool) > 1)
            with verrou:
                if issue == _REUSED:
                    report.n_reused += 1
                elif issue == _OK:
                    report.n_downloaded += 1
                    report.bytes_written += ecrits
                elif retry and rejouable:
                    retombees[ref.beatmap_id] = mirror
                elif issue == _MISMATCH:
                    report.n_checksum_mismatch += 1
                    report.failures.append(ref.beatmap_id)
                else:
                    report.n_failed += 1
                    report.failures.append(ref.beatmap_id)
                # une map renvoyee au rattrapage sera comptee la-bas
                if bar and not (retry and rejouable):
                    bar.update(1)
            time.sleep(pause)

    def _lancer(tireurs: dict[str, object], pause: float,
                retry: bool) -> dict[int, Mirror]:
        retombees: dict[int, Mirror] = {}
        fils = [
            threading.Thread(
                target=_consommer,
                args=(mirror, tireurs[mirror.name], pause, retry, retombees),
                daemon=True,
            )
            for mirror in pool
            if mirror.name in tireurs
        ]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()
        return retombees

    def _passe_partagee(batch, pause: float, retry: bool) -> dict[int, Mirror]:
        """
        Passe principale : une seule file, tous les mirrors tirent dedans.

        C'est ce qui equilibre la charge tout seul -- un mirror rapide sert plus
        de maps qu'un mirror lent, sans qu'on ait a mesurer quoi que ce soit.
        """
        travail: queue.Queue = queue.Queue()
        for ref in batch:
            travail.put(ref)

        def tirer():
            try:
                return travail.get_nowait()
            except queue.Empty:
                return None

        return _lancer({mirror.name: tirer for mirror in pool}, pause, retry)

    def _passe_rattrapage(tombees: dict[int, Mirror], pause: float) -> None:
        """
        Passe de rattrapage : chaque map est assignee d'avance a un mirror
        DIFFERENT de celui qui a echoue, et chaque worker ne vide que sa part.

        Une file partagee ne conviendrait pas ici : il faudrait remettre dans la
        file les maps qu'un worker doit eviter, et si l'autre thread a deja fini
        de tirer, celui qui reste les remettrait indefiniment. Assigner en amont
        supprime le probleme au lieu de le rattraper.
        """
        lots: dict[str, list[BeatmapRef]] = {mirror.name: [] for mirror in pool}
        for rang, (bid, fautif) in enumerate(tombees.items()):
            autres = [m for m in pool if m is not fautif] or list(pool)
            lots[autres[rang % len(autres)].name].append(par_id[bid])

        def faire_tireur(nom: str):
            reste = lots[nom]
            def tirer():
                return reste.pop() if reste else None
            return tirer

        _lancer({nom: faire_tireur(nom) for nom, l in lots.items() if l},
                pause, retry=False)

    try:
        tombees = _passe_partagee(refs, delay, retry=retry_pass)
        if tombees:
            if bar:
                bar.set_postfix_str(f"rattrapage de {len(tombees)}")
            _passe_rattrapage(tombees, delay * RETRY_PASS_SLOWDOWN)
    finally:
        if bar:
            bar.close()

    report.elapsed_s = time.perf_counter() - started
    return report


# --- manifeste ----------------------------------------------------------------

def write_manifest(
    out_dir: Path,
    refs: Sequence[BeatmapRef],
    *,
    mirror: Mirror,
    statuses: Sequence[str],
    report: FetchReport,
    merge: bool = True,
    criteria: dict[str, Any] | None = None,
) -> Path:
    """
    Trace ce qui a ete pris, quand, et d'ou.

    Le playcount et le statut ne figurent pas dans le .osu : ce fichier est la
    seule source des colonnes `status` et `playcount` de l'index. La date compte
    aussi -- une map graveyard peut etre reuploadee par son auteur, son md5
    change alors, et l'entree du pool devient silencieusement obsolete.

    **Fusion, pas ecrasement.** Le dossier de sortie s'accumule d'un passage a
    l'autre : un fichier telecharge par un lot precedent y reste, meme si le lot
    d'aujourd'hui a d'autres criteres et ne le redemande pas. Un manifeste
    reecrit a neuf perdrait son annotation, et `build_index` le prendrait alors
    pour une map du dump -- il etiquetterait `ranked` une map graveyard, sans
    rien signaler. On repart donc de ce qui est deja ecrit, et on n'y superpose
    que le lot courant.

    Ne survivent a la fusion que les entrees dont le `.osu` est encore la : un
    manifeste ne doit decrire que ce que le dossier contient vraiment.
    """
    ecartes = set(report.failures)
    beatmaps: dict[str, dict[str, Any]] = {}

    destination = out_dir / MANIFEST_FILE
    if merge and destination.is_file():
        try:
            ancien = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ancien = {}
        for cle, valeur in (ancien.get("beatmaps") or {}).items():
            if (out_dir / f"{cle}.osu").is_file():
                beatmaps[str(cle)] = valeur

    n_repris = len(beatmaps)
    for ref in refs:
        if ref.beatmap_id in ecartes:
            continue
        beatmaps[str(ref.beatmap_id)] = {
            "beatmapset_id": ref.beatmapset_id,
            "checksum": ref.checksum,
            "status": ref.status,
            "playcount": ref.playcount,
            "stars": round(ref.stars, 3),
            "label": ref.label,
        }

    payload = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mirror": mirror.name,
        "statuses": list(statuses),
        "criteria": criteria or {},
        "n_refs": len(refs),
        "n_available": sum(1 for r in refs if r.beatmap_id not in ecartes),
        "n_carried_over": n_repris,
        "n_total": len(beatmaps),
        "beatmaps": beatmaps,
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination


def write_loved(path: Path, checksums: set[str]) -> Path:
    """Ecrit la liste des md5 loved, avec sa date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "n": len(checksums),
                "checksums": sorted(checksums),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_loved(source: Path) -> set[str]:
    """
    Relit la liste des md5 loved. Ensemble vide si le fichier n'est pas la.

    L'absence n'est pas une erreur : sans cette liste, les maps du dump gardent
    le statut `dump`, qui ne ment sur rien -- il dit juste qu'on ne sait pas
    separer ranked de loved.
    """
    path = Path(source)
    if path.is_dir():
        path = path / LOVED_FILE
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(c).lower() for c in (payload.get("checksums") or [])}


def load_annotations(source: Path) -> dict[str, dict[str, Any]]:
    """
    Relit un manifeste en `checksum -> {status, playcount, beatmap_id}`.

    La cle est le checksum et pas le beatmap_id, alors que le fichier s'appelle
    pourtant `{beatmap_id}.osu`. Raison mesuree sur un lot reel : le graveyard
    contient des .osu dont le champ `BeatmapID:` ne correspond pas a leur propre
    identifiant, parce que le mappeur a copie une difficulte d'ailleurs sans
    nettoyer les metadonnees. Une jointure sur beatmap_id etiquetait alors
    silencieusement la map comme venant du dump.

    Le checksum, lui, ne peut pas mentir : c'est le md5 des octets, celui-la
    meme qu'on a verifie au telechargement, et la cle primaire du corpus.

    Accepte le dossier ou le fichier directement, et renvoie un dict vide si
    rien n'est la : construire un index sans maps non classees doit rester
    possible sans condition particuliere chez l'appelant.
    """
    path = Path(source)
    if path.is_dir():
        path = path / MANIFEST_FILE
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    annotations: dict[str, dict[str, Any]] = {}
    for key, value in (payload.get("beatmaps") or {}).items():
        checksum = str(value.get("checksum") or "").lower()
        if not checksum:
            continue
        annotations[checksum] = {
            "status": value.get("status") or "unknown",
            "playcount": int(value.get("playcount") or 0),
            "beatmap_id": int(key),
        }
    return annotations
