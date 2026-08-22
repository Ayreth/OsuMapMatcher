#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corpus.py - construction du corpus de features sur tout le dump.

Ecriture incrementale par tranches en parquet, multiprocessing, et reprise sur
interruption : une tranche n'est consideree terminee que quand son fichier de
stats existe, ce qui n'arrive qu'apres l'ecriture reussie du parquet.

Deux sources possibles :
  * un dossier de .osu deja extraits (cas normal ici) ;
  * une archive .tar.bz2 lue en streaming, sans extraction sur disque.
"""

from __future__ import annotations

import json
import math
import os
import tarfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from .features import FEATURE_NAMES, META_NAMES, featurize_detailed

COLUMNS: list[str] = list(META_NAMES) + list(FEATURE_NAMES)
DEFAULT_CHUNK_SIZE = 50_000
_INT_COLUMNS = ("beatmap_id", "beatmapset_id")


# --- workers (doivent rester au niveau module pour etre picklables) -----------

#: mods du worker courant, pose une fois par processus par `_init_worker`
_WORKER_MODS: object | None = None


def _init_worker(mods_spec: object) -> None:
    global _WORKER_MODS
    from .features import Mods

    _WORKER_MODS = Mods.parse(mods_spec)


def _work_path(path: str) -> tuple[dict | None, str | None]:
    location = Path(path)
    try:
        raw = location.read_bytes()
    except OSError as exc:
        return None, f"io:{type(exc).__name__}"
    result = featurize_detailed(raw, _WORKER_MODS)
    features = result.features
    # Un fichier sur sept du dump a `BeatmapID:0` dans ses metadonnees alors que
    # son nom de fichier porte l'identifiant. On rattrape ici plutot que dans
    # featurize(), qui doit rester une fonction pure des octets : en live, un
    # id a zero est une vraie information (map non soumise).
    if features is not None and not features["beatmap_id"] and location.stem.isdigit():
        features["beatmap_id"] = int(location.stem)
    return features, result.skip_reason


def _work_bytes(raw: bytes) -> tuple[dict | None, str | None]:
    result = featurize_detailed(raw, _WORKER_MODS)
    return result.features, result.skip_reason


# --- etat d'une tranche -------------------------------------------------------

@dataclass
class ChunkPaths:
    parquet: Path
    stats: Path

    @property
    def done(self) -> bool:
        # le json de stats n'est ecrit qu'apres le parquet : sa presence est la
        # seule preuve qu'une tranche est complete
        return self.stats.exists()


def _chunk_paths(out_dir: Path, index: int) -> ChunkPaths:
    return ChunkPaths(
        parquet=out_dir / f"chunk_{index:05d}.parquet",
        stats=out_dir / f"chunk_{index:05d}.stats.json",
    )


def _write_chunk(rows: list[dict], skips: Counter, out_dir: Path, index: int) -> None:
    paths = _chunk_paths(out_dir, index)
    if rows:
        frame = pd.DataFrame(rows, columns=COLUMNS)
        frame[list(FEATURE_NAMES)] = frame[list(FEATURE_NAMES)].astype("float32")
        frame["stars"] = frame["stars"].astype("float32")
        for column in _INT_COLUMNS:
            frame[column] = frame[column].astype("int64")
        temporary = paths.parquet.with_suffix(".parquet.tmp")
        frame.to_parquet(temporary, index=False)
        temporary.replace(paths.parquet)
    paths.stats.write_text(
        json.dumps({"n_rows": len(rows), "skips": dict(skips)}), encoding="utf-8"
    )


def _read_chunk_stats(out_dir: Path, index: int) -> tuple[int, Counter]:
    paths = _chunk_paths(out_dir, index)
    payload = json.loads(paths.stats.read_text(encoding="utf-8"))
    return int(payload["n_rows"]), Counter(payload["skips"])


# --- construction -------------------------------------------------------------

@dataclass
class BuildReport:
    n_seen: int = 0
    n_kept: int = 0
    skips: Counter = field(default_factory=Counter)
    n_chunks: int = 0
    n_reused: int = 0
    elapsed_s: float = 0.0

    def render(self) -> str:
        lines = [
            "=" * 70,
            "RAPPORT DE CONSTRUCTION",
            "=" * 70,
            f"  fichiers vus     : {self.n_seen}",
            f"  maps retenues    : {self.n_kept}",
            f"  tranches ecrites : {self.n_chunks - self.n_reused}"
            f"  (dont {self.n_reused} reprises telles quelles)",
            f"  duree            : {self.elapsed_s:.0f} s",
            "",
            "  skips par raison :",
        ]
        if self.skips:
            for reason, count in self.skips.most_common():
                share = 100.0 * count / max(self.n_seen, 1)
                lines.append(f"     {reason:<26} {count:>8}  ({share:5.2f} %)")
        else:
            lines.append("     aucun")
        lines.append("=" * 70)
        return "\n".join(lines)


def _batched(iterable: Iterable, size: int) -> Iterator[list]:
    batch: list = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _iter_tar_members(archive: Path) -> Iterator[bytes]:
    """Streame l'archive membre par membre, sans jamais l'extraire sur disque."""
    with tarfile.open(archive, mode="r:bz2") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".osu"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            yield handle.read()
            tar.members = []  # sinon tarfile garde tout l'index en RAM


def _clear_chunks(out_dir: Path) -> int:
    """Efface les tranches d'un lot precedent. Renvoie combien de fichiers."""
    morts = sorted(out_dir.glob("chunk_*.parquet")) + sorted(
        out_dir.glob("chunk_*.stats.json")
    )
    for path in morts:
        path.unlink(missing_ok=True)
    return len(morts)


def resume_is_valid(out_dir: Path, source: Path, mods: object | None) -> bool:
    """
    Les tranches deja presentes decrivent-elles bien CE lot ?

    La reprise se fie a l'existence de `chunk_N.stats.json`, ce qui ne dit rien
    de ce qu'il y a dedans. Un corpus bati sur un dossier et relance sur un
    autre reutilisait donc ses vieilles tranches sans un mot, en rapportant
    leurs statistiques contre le nouveau nombre de fichiers -- d'ou des taux de
    skip au-dessus de 100 %, seul symptome visible. C'est arrive en vrai : le
    corpus du dump s'est retrouve construit sur le pool non classe, et le
    resultat avait l'air normal.

    On compare donc la source et les mods enregistres avant de faire confiance
    a quoi que ce soit.
    """
    manifest = out_dir / "manifest.json"
    if not manifest.is_file():
        return True  # rien a contredire
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    if str(payload.get("mods", "")) != str(mods or ""):
        return False
    if Path(payload.get("source", "")) != Path(source):
        return False

    # Le manifeste se contredit-il ? Chaque fichier vu est soit garde soit
    # skippe, donc `n_kept + skips == n_seen`, toujours. Quand une tranche
    # perimee est reutilisee, ses compteurs a elle sont rapportes contre le
    # nombre de fichiers du NOUVEAU lot, et l'egalite casse. C'est le meme
    # signal que les taux de skip au-dessus de 100 % -- sauf qu'ici on le teste
    # au lieu de se contenter de l'afficher.
    #
    # C'est le seul garde-fou qui rattrape le cas ou la source et le nombre de
    # fichiers sont pourtant corrects : un dossier qui a grossi depuis le
    # dernier passage, avec une tranche qui ne couvre que l'ancien contenu.
    n_seen = int(payload.get("n_seen", 0))
    n_kept = int(payload.get("n_kept", 0))
    n_skipped = sum(int(v) for v in (payload.get("skips") or {}).values())
    return n_kept + n_skipped == n_seen


def build_corpus(
    source: Path,
    out_dir: Path,
    *,
    workers: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    limit: int | None = None,
    resume: bool = True,
    progress: bool = True,
    mods: object | None = None,
) -> BuildReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    if resume and not resume_is_valid(out_dir, source, mods):
        print(f"!! {out_dir} a ete bati sur une autre source ou d'autres mods.")
        print("!! Ses tranches sont effacees et tout est refeaturise.")
        resume = False
    if not resume:
        # Ne pas se contenter de les ignorer : `load_corpus` ramasse tous les
        # `chunk_*.parquet` du dossier. Un lot plus petit que le precedent
        # laisserait des tranches orphelines derriere lui, et l'index les
        # avalerait sans que rien ne le signale.
        _clear_chunks(out_dir)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    report = BuildReport()
    started = time.perf_counter()

    from_tar = source.is_file()
    if from_tar:
        batches: Iterator[list] = _batched(_iter_tar_members(source), chunk_size)
        worker, total = _work_bytes, None
    else:
        files = sorted(source.glob("*.osu"))
        if limit:
            files = files[:limit]
        total = len(files)
        batches = _batched((str(p) for p in files), chunk_size)
        worker = _work_path

    if progress:
        try:
            from tqdm import tqdm
        except ImportError:
            tqdm = None
    else:
        tqdm = None

    bar = tqdm(total=total, unit="map", smoothing=0.05) if tqdm else None
    print(f"source  : {source}")
    print(f"sortie  : {out_dir}")
    print(f"workers : {workers}   tranche : {chunk_size}   mods : {mods or 'nomod'}"
          + (f"   fichiers : {total}" if total else "   (streaming tar)"))

    try:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(mods,)
        ) as pool:
            for index, batch in enumerate(batches):
                paths = _chunk_paths(out_dir, index)
                if resume and paths.done:
                    n_rows, skips = _read_chunk_stats(out_dir, index)
                    report.n_kept += n_rows
                    report.skips.update(skips)
                    report.n_seen += len(batch)
                    report.n_chunks += 1
                    report.n_reused += 1
                    if bar:
                        bar.update(len(batch))
                        bar.set_postfix_str(f"tranche {index} deja faite")
                    continue

                rows: list[dict] = []
                skips = Counter()
                for features, reason in pool.map(worker, batch, chunksize=64):
                    if features is None:
                        skips[reason or "inconnu"] += 1
                    else:
                        rows.append(features)
                    if bar:
                        bar.update(1)

                _write_chunk(rows, skips, out_dir, index)
                report.n_kept += len(rows)
                report.n_seen += len(batch)
                report.skips.update(skips)
                report.n_chunks += 1
                if bar:
                    bar.set_postfix_str(f"tranche {index}: {len(rows)} gardees")
    finally:
        if bar:
            bar.close()

    report.elapsed_s = time.perf_counter() - started
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "mods": str(mods or ""),
                "chunk_size": chunk_size,
                "n_chunks": report.n_chunks,
                "n_seen": report.n_seen,
                "n_kept": report.n_kept,
                "skips": dict(report.skips),
                "columns": COLUMNS,
                "n_features": len(FEATURE_NAMES),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report


def load_corpus(out_dir: Path) -> pd.DataFrame:
    """Recharge toutes les tranches dans un seul DataFrame (phase 4)."""
    parts = sorted(out_dir.glob("chunk_*.parquet"))
    if not parts:
        raise FileNotFoundError(f"aucune tranche parquet dans {out_dir}")
    return pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
