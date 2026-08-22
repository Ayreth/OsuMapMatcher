#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_index.py - install index variants on a machine that will serve the API.

The launcher does this for the single `nomod` variant when a player starts the
tool. A server usually wants more than one, and wants it without a browser, a
game or a prompt -- hence this script.

    python scripts/fetch_index.py                      # nomod
    python scripts/fetch_index.py --variants nomod,DT,HR,DTHR,HT
    OSUMAPMATCHER_INDEX_URL=https://cdn.example/osu-mapmatcher/ python scripts/fetch_index.py

The URL is resolved exactly like the launcher's: OSUMAPMATCHER_INDEX_URL first (a
full URL for one archive, or a folder ending in "/" holding
osu-mapmatcher-index-<variant>.zip), then this clone's git remote, then upstream.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osumapmatcher.launcher import ensure_index, has_index, index_url  # noqa: E402
from osumapmatcher.paths import index_root  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Download prebuilt index variants")
    parser.add_argument("--variants", default="nomod",
                        help="comma-separated: nomod,DT,HR,DTHR,HT")
    parser.add_argument("--check", action="store_true",
                        help="only report what is installed, download nothing")
    args = parser.parse_args()

    wanted = [name.strip() for name in args.variants.split(",") if name.strip()]
    print(f"index folder: {index_root()}")

    if args.check:
        missing = [name for name in wanted if not has_index(name)]
        for name in wanted:
            state = "present" if has_index(name) else f"missing  <- {index_url(name)}"
            print(f"  {name:6s} {state}")
        return 1 if missing else 0

    return 0 if ensure_index(wanted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
