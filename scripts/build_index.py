#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_index.py - phase 4. Scaler + vecteurs + index ANN + SQLite.

Usage :
    .venv\\Scripts\\python.exe scripts\\build_index.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.index import build  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Construction de l'index (phase 4)")
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus"))
    parser.add_argument("--out", type=Path, default=Path("data/index"))
    parser.add_argument("--clip", type=float, default=5.0,
                        help="ecretage des valeurs scalees, en unites robustes")
    args = parser.parse_args()

    started = time.perf_counter()
    report = build(args.corpus, args.out, clip=args.clip)
    print(f"maps indexees : {report['n_maps']}")
    print(f"features      : {report['n_features']}")
    print(f"vecteurs      : {report['vectors_mb']} Mo")
    print(f"index ANN     : {report['index_mb']} Mo")
    print(f"sqlite        : {report['sqlite_mb']} Mo")
    print(f"duree         : {time.perf_counter() - started:.0f} s")
    print(f"sortie        : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
