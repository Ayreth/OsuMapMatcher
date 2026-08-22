# -*- coding: utf-8 -*-
"""
Fixtures partagees.

`mini_index` fabrique un index complet mais minuscule dans un dossier
temporaire : les tests de recherche et d'API tournent donc sur une machine
neuve, sans les 1,1 Go de data/index et sans le dump.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: assez de maps pour que filtres et dedoublonnage aient de quoi mordre, assez
#: peu pour que la featurisation tienne en une seconde
MINI_CORPUS = 24


def _mini_rows() -> list[tuple[bytes, dict]]:
    """Un petit corpus de .osu synthetiques, tous distincts."""
    # import differe : pytest ajoute tests/ a sys.path, et make_osu vit avec les
    # tests de features -- une seule definition du generateur de .osu
    from test_features import make_osu

    from osureco.features import featurize

    rows: list[tuple[bytes, dict]] = []
    for i in range(MINI_CORPUS):
        raw = make_osu(n_objects=80 + 4 * i, spacing_ms=300 + 10 * i)
        # titres et identifiants distincts, sinon le dedoublonnage par chanson
        # ne renverrait qu'un seul resultat
        raw = raw.replace(b"Title:Synthetic", f"Title:Synthetic {i}".encode())
        raw = raw.replace(b"BeatmapID:12345", f"BeatmapID:{1000 + i}".encode())
        raw = raw.replace(b"BeatmapSetID:678", f"BeatmapSetID:{500 + i}".encode())
        features = featurize(raw)
        assert features is not None, "le .osu synthetique doit etre featurisable"
        rows.append((raw, features))
    return rows


def _write_variant(directory: Path, rows: list[tuple[bytes, dict]]) -> None:
    import numpy as np
    from usearch.index import Index

    from osureco.features import FEATURE_NAMES
    from osureco.index import (
        DB_FILE,
        INDEX_FILE,
        RAW_FILE,
        SCALER_FILE,
        VECTORS_FILE,
        Scaler,
        _SCHEMA,
    )

    directory.mkdir(parents=True, exist_ok=True)
    features = [row[1] for row in rows]
    matrix = np.array(
        [[float(f[name]) for name in FEATURE_NAMES] for f in features],
        dtype=np.float64,
    )
    scaler = Scaler.fit(matrix)
    scaler.save(directory / SCALER_FILE)

    # ordre FORTRAN volontaire : c'est la disposition que produit
    # `DataFrame.to_numpy()`, donc celle des index deja distribues, et c'est
    # elle qui a casse la recherche ANN
    vectors = scaler.transform(matrix)
    np.save(directory / VECTORS_FILE, np.asfortranarray(vectors))
    np.save(directory / RAW_FILE, np.asfortranarray(matrix.astype(np.float32)))

    connection = sqlite3.connect(directory / DB_FILE)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO maps (pos, checksum, beatmap_id, beatmapset_id, artist,"
            " title, creator, version, stars, bpm, length_s, n_objects)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    position,
                    f["checksum"], int(f["beatmap_id"]), int(f["beatmapset_id"]),
                    f["artist"], f["title"], f["creator"], f["version"],
                    float(f["stars"]), float(f["bpm_median"]),
                    float(f["drain_time_s"]), int(f["n_objects"]),
                )
                for position, f in enumerate(features)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    index = Index(ndim=vectors.shape[1], metric="l2sq", dtype="f32")
    index.add(
        np.arange(len(vectors), dtype=np.int64),
        np.ascontiguousarray(vectors, dtype=np.float32),
        threads=0,
    )
    index.save(directory / INDEX_FILE)


@pytest.fixture(scope="session")
def mini_corpus() -> list[tuple[bytes, dict]]:
    return _mini_rows()


@pytest.fixture(scope="session")
def mini_index(tmp_path_factory, mini_corpus) -> Path:
    """Racine d'index contenant la seule variante `nomod`."""
    root = tmp_path_factory.mktemp("index")
    _write_variant(root / "nomod", mini_corpus)
    return root
