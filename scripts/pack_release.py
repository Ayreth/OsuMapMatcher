#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pack_release.py - bundle an index variant into the archive served by releases.

The launcher downloads this file and extracts it straight into data/index/, so
the archive must contain the variant folder itself (nomod/scaler.npz, ...).

    .venv\\Scripts\\python.exe scripts\\pack_release.py
    .venv\\Scripts\\python.exe scripts\\pack_release.py --variant DT
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.launcher import REQUIRED  # noqa: E402
from osumapmatcher.paths import data_dir, index_root  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Pack an index for a GitHub release")
    parser.add_argument("--variant", default="nomod")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    source = index_root() / args.variant
    missing = [name for name in REQUIRED if not (source / name).is_file()]
    if missing:
        print(f"{source} is not a complete index, missing: {', '.join(missing)}")
        return 1

    out = args.out or (data_dir() / "release" / f"osu-mapmatcher-index-{args.variant}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in source.iterdir() if p.is_file())
    raw_total = sum(p.stat().st_size for p in files)
    print(f"packing {len(files)} files ({raw_total / 1e6:.0f} MB) from {source}")

    started = time.perf_counter()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            print(f"  {path.name} ({path.stat().st_size / 1e6:.0f} MB)…", flush=True)
            bundle.write(path, arcname=f"{args.variant}/{path.name}")

    size = out.stat().st_size
    print()
    print(f"wrote {out}")
    print(f"  {size / 1e6:.0f} MB  ({100 * size / raw_total:.0f} % of the raw size)"
          f"  in {time.perf_counter() - started:.0f} s")
    print()
    print("Upload it as a release asset named exactly:")
    print(f"  osu-mapmatcher-index-{args.variant}.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
