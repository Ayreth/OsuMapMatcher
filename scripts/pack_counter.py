#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pack_counter.py - build the tosu counter from the one page there is.

The page in `src/osumapmatcher/static/index.html` works under either host: served by
the local Python server, or served by tosu itself. This script produces the
folder to drop into tosu, so there is no second copy of the interface to keep
in sync -- only a generated artefact.

    python scripts/pack_counter.py                        # -> counter/
    python scripts/pack_counter.py --install              # -> tosu/static/
    python scripts/pack_counter.py --api https://mine.example

Installing means copying the folder into tosu's `static/` directory; tosu then
serves it at http://127.0.0.1:24050/<folder>/ and lists it in its dashboard.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.paths import PROJECT_ROOT  # noqa: E402

FOLDER = "osu-mapmatcher"
PAGE = PROJECT_ROOT / "src" / "osumapmatcher" / "static" / "index.html"

#: decrit le counter pour la bibliotheque tosu. Le counter fonctionne sans,
#: c'est la fiche de la vitrine, pas une dependance.
METADATA = {
    "name": "osu!MapMatcher",
    "author": "Ayreth",
    "resolution": [-1, -1],
    "authorLinks": ["https://github.com/Ayreth/OsuMapMatcher"],
    "version": "1.0.0",
}


def find_tosu() -> Path | None:
    """Le dossier static de tosu, s'il est installe a cote du depot."""
    for candidate in (PROJECT_ROOT / "tosu" / "static", PROJECT_ROOT.parent / "tosu" / "static"):
        if candidate.is_dir():
            return candidate
    return None


def build(destination: Path, api: str) -> Path:
    page = PAGE.read_text(encoding="utf-8")
    if api:
        marker = 'const DEFAULT_API = "'
        start = page.index(marker) + len(marker)
        end = page.index('"', start)
        page = page[:start] + api + page[end:]

    target = destination / FOLDER
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text(page, encoding="utf-8")
    (target / "metadata.txt").write_text(
        json.dumps(METADATA, indent=2), encoding="utf-8"
    )
    return target


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Build the tosu counter")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "counter")
    parser.add_argument("--api", default="", help="API the counter will query")
    parser.add_argument("--install", action="store_true",
                        help="also copy it into tosu's static folder")
    args = parser.parse_args()

    built = build(args.out, args.api)
    size = sum(p.stat().st_size for p in built.iterdir())
    print(f"counter built in {built}  ({size / 1024:.0f} KB)")

    if args.install:
        static = find_tosu()
        if static is None:
            print("tosu not found next to the repository; copy the folder by hand:")
            print(f"  {built}  ->  <tosu>/static/{FOLDER}")
            return 1
        destination = static / FOLDER
        shutil.copytree(built, destination, dirs_exist_ok=True)
        print(f"installed in {destination}")
        print(f"open http://127.0.0.1:24050/{FOLDER}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
