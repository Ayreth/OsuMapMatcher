#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
featurize_sample.py - critere de sortie de la phase 2.

Tire N fichiers .osu au hasard dans le dump, les passe dans featurize(), et
rend compte : DataFrame produit, comptage des skips par raison, controle de
sante des colonnes, et verification du determinisme.

Usage :
    .venv\\Scripts\\python.exe scripts\\featurize_sample.py
    .venv\\Scripts\\python.exe scripts\\featurize_sample.py --n 1000 --seed 7
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osureco.features import (  # noqa: E402
    FEATURE_GROUPS,
    FEATURE_NAMES,
    GROUP_NAMES,
    META_NAMES,
    feature_vector,
    featurize_detailed,
)

from osureco.paths import data_dir, describe_dump, find_dump  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Echantillon de featurisation (phase 2)")
    parser.add_argument("--dump", type=Path, default=None,
                        help="dossier de .osu (auto-detecte si omis)")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    dump = args.dump or find_dump()
    if dump is None or not dump.is_dir():
        print(describe_dump())
        return 1
    out_path = args.out or (data_dir() / "sample_features.parquet")

    print(f"listing de {dump} ...", flush=True)
    all_files = sorted(dump.glob("*.osu"))
    print(f"{len(all_files)} fichiers .osu dans le dump")

    rng = random.Random(args.seed)
    sample = rng.sample(all_files, min(args.n, len(all_files)))
    print(f"echantillon : {len(sample)} fichiers (seed={args.seed})\n")

    rows: list[dict] = []
    skips: Counter = Counter()
    skip_examples: dict[str, str] = {}
    t0 = time.perf_counter()

    for path in sample:
        result = featurize_detailed(path.read_bytes())
        if result.features is None:
            skips[result.skip_reason] += 1
            skip_examples.setdefault(result.skip_reason, path.name)
            continue
        rows.append(result.features)

    elapsed = time.perf_counter() - t0

    # ---- rapport de skips ----------------------------------------------------
    print("=" * 78)
    print("SKIPS")
    print("=" * 78)
    if skips:
        for reason, count in skips.most_common():
            pct = 100.0 * count / len(sample)
            print(f"  {reason:<28} {count:>5}  ({pct:5.1f} %)   ex: {skip_examples[reason]}")
    else:
        print("  aucun")
    print(f"\n  traites {len(rows)} / {len(sample)}  en {elapsed:.1f} s "
          f"({1000 * elapsed / max(len(sample), 1):.1f} ms par fichier)")

    if not rows:
        print("\nAucune map featurisee, rien a analyser.")
        return 1

    # ---- DataFrame -----------------------------------------------------------
    df = pd.DataFrame(rows, columns=list(META_NAMES) + list(FEATURE_NAMES))
    print()
    print("=" * 78)
    print("DATAFRAME")
    print("=" * 78)
    print(f"  shape          : {df.shape}  ({len(FEATURE_NAMES)} features + "
          f"{len(META_NAMES)} colonnes meta)")
    print(f"  features par groupe :")
    for group in GROUP_NAMES:
        names = [n for n in FEATURE_NAMES if FEATURE_GROUPS[n] == group]
        print(f"     {group:<12} {len(names):>3}")

    matrix = np.vstack([feature_vector(row) for row in rows])
    print(f"  matrice        : {matrix.shape} {matrix.dtype}")

    # ---- controles de sante --------------------------------------------------
    print()
    print("=" * 78)
    print("SANTE DES COLONNES")
    print("=" * 78)
    n_nan = int(np.isnan(matrix).sum())
    n_inf = int(np.isinf(matrix).sum())
    print(f"  NaN : {n_nan}     inf : {n_inf}")

    constant = [FEATURE_NAMES[i] for i in range(matrix.shape[1])
                if float(np.ptp(matrix[:, i])) == 0.0]
    print(f"  colonnes constantes sur l'echantillon : {len(constant)}")
    for name in constant:
        print(f"     {name}  (= {matrix[0, list(FEATURE_NAMES).index(name)]:.4g})")

    zero_iqr = []
    for i, name in enumerate(FEATURE_NAMES):
        q75, q25 = np.percentile(matrix[:, i], [75, 25])
        if q75 - q25 == 0 and float(np.ptp(matrix[:, i])) != 0.0:
            zero_iqr.append(name)
    print(f"  colonnes a IQR nul (RobustScaler divisera par 0) : {len(zero_iqr)}")
    for name in zero_iqr:
        print(f"     {name}")

    # ---- determinisme --------------------------------------------------------
    print()
    print("=" * 78)
    print("DETERMINISME")
    print("=" * 78)
    identical = True
    for path in sample[:20]:
        raw = path.read_bytes()
        a = featurize_detailed(raw)
        b = featurize_detailed(raw)
        if a.features is None or b.features is None:
            continue
        if not np.array_equal(feature_vector(a.features), feature_vector(b.features)):
            identical = False
            print(f"  DIVERGENCE sur {path.name}")
    print(f"  20 fichiers featurises deux fois : "
          f"{'identiques' if identical else 'DIVERGENTS'}")

    # ---- apercu --------------------------------------------------------------
    print()
    print("=" * 78)
    print("APERCU (5 maps)")
    print("=" * 78)
    preview = df[["artist", "title", "version", "stars", "bpm_median",
                  "drain_time_s", "object_density", "aim_spike", "speed_spike",
                  "sp_p50", "ang_150_180"]].head(5)
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(preview.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print()
    print("distribution de quelques features cles :")
    describe_cols = ["stars", "bpm_median", "object_density", "sp_p50", "vel_p50",
                     "aim_spike", "speed_spike", "aim_hot", "dt_frac_lt100",
                     "aimns_over_aim", "ang_150_180"]
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(df[describe_cols].describe().to_string(float_format=lambda v: f"{v:.3f}"))

    # ---- ecriture parquet ----------------------------------------------------
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    reread = pd.read_parquet(out_path)
    ok = reread.shape == df.shape and list(reread.columns) == list(df.columns)
    print()
    print(f"parquet ecrit : {out_path}  ({out_path.stat().st_size / 1024:.0f} Ko), "
          f"relecture {'OK' if ok else 'KO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
