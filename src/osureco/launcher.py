#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher.py - one-click entry point.

Makes sure an index exists, then starts the local server and opens the browser.

The index is the only heavy piece: ~150 MB of numbers and metadata derived from
the public ppy beatmap dump. Building it from scratch means downloading ~3 GB
and burning a couple of minutes of CPU, so by default we fetch a prebuilt one
from the repository's GitHub releases instead. `--build` does it locally.

Where the index comes from, in order of precedence:

  1. OSURECO_INDEX_URL, if set (a self-hosted mirror, for instance)
  2. this clone's git remote, so a fork serves its own index with no code change
  3. the upstream repository below

Step 3 exists because most people click "Download ZIP" rather than cloning, and
a ZIP has no .git folder to read a remote from.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

from .paths import PROJECT_ROOT, data_dir, describe_dump, find_dump, index_root

ASSET_NAME = "osureco-index-nomod.zip"
#: last-resort source, used when there is no git remote to read (ZIP downloads)
UPSTREAM_SLUG = "Ayreth/OsuMapMatcher"
#: files that must all be present for a variant folder to count as usable
REQUIRED = ("scaler.npz", "vectors.npy", "vectors_raw.npy", "index.usearch", "corpus.sqlite")


# --- prebuilt index -----------------------------------------------------------

def remote_slug() -> str | None:
    """`owner/repo` read straight from .git/config, without needing git on PATH."""
    config = PROJECT_ROOT / ".git" / "config"
    if not config.is_file():
        return None
    text = config.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"url\s*=\s*\S*github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?\s*$",
        text, re.MULTILINE,
    )
    return f"{match.group(1)}/{match.group(2)}" if match else None


def index_url() -> str:
    override = os.environ.get("OSURECO_INDEX_URL")
    if override:
        return override
    slug = remote_slug() or UPSTREAM_SLUG
    return f"https://github.com/{slug}/releases/latest/download/{ASSET_NAME}"


def has_index(variant: str = "nomod") -> bool:
    folder = index_root() / variant
    return all((folder / name).is_file() for name in REQUIRED)


def _report(done: int, total: int) -> None:
    if total > 0:
        share = 100.0 * done / total
        print(f"\r  {share:5.1f} %  ({done / 1e6:.0f} / {total / 1e6:.0f} MB)",
              end="", flush=True)
    else:
        print(f"\r  {done / 1e6:.0f} MB", end="", flush=True)


def download_index(url: str) -> bool:
    print(f"Downloading the prebuilt index from:\n  {url}")
    target = index_root()
    target.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "osureco-launcher"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                archive = Path(tmp.name)
                done = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    done += len(chunk)
                    _report(done, total)
        print()
    except Exception as exc:
        print(f"\n  download failed: {type(exc).__name__}: {exc}")
        return False

    try:
        print("Extracting…")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
    except Exception as exc:
        print(f"  the archive could not be read: {type(exc).__name__}: {exc}")
        return False
    finally:
        archive.unlink(missing_ok=True)

    if not has_index():
        print("  the archive did not contain a usable index.")
        return False
    print(f"Index ready in {target}")
    return True


# --- local build --------------------------------------------------------------

def build_index(variants: str) -> bool:
    from .corpus import build_corpus
    from .index import build
    from .paths import corpus_root

    dump = find_dump()
    if dump is None:
        print(describe_dump())
        return False

    print(f"Building from {dump}")
    for variant in [v.strip() for v in variants.split(",") if v.strip()]:
        mods = None if variant == "nomod" else variant
        print(f"\n--- {variant} ---")
        report = build_corpus(dump, corpus_root() / variant, mods=mods)
        print(report.render())
        stats = build(corpus_root() / variant, index_root() / variant)
        print(f"{variant}: {stats['n_maps']} maps indexed")
    return has_index()


# --- entry point --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Start osu!MapMatcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--build", action="store_true",
                        help="build the index locally instead of downloading it")
    parser.add_argument("--variants", default="nomod",
                        help="mod corpora to build with --build (nomod,DT,HR,DTHR,HT)")
    parser.add_argument("--no-browser", dest="browser", action="store_false")
    args = parser.parse_args(argv)

    print("osu!MapMatcher")
    print(f"  data folder : {data_dir()}")

    if args.build:
        if not build_index(args.variants):
            return 1
    elif not has_index():
        print("\nNo index found yet — this is a one-time setup.")
        url = index_url()
        if not url or not download_index(url):
            if not url:
                print("\nCould not work out where to download the index from "
                      "(no GitHub remote in .git/config).")
            print(
                "\nFall back to building it yourself:\n"
                "  1. grab a *_osu_files.tar.bz2 dump from https://data.ppy.sh\n"
                f"  2. extract it into {data_dir() / 'osu_files'}\n"
                "  3. run this again with --build\n"
            )
            return 1
    else:
        print("  index       : already installed")

    # imported late so a missing index fails with our message, not a stack trace
    import uvicorn

    from .server import create_app

    address = f"http://{args.host}:{args.port}"
    print(f"\nServer: {address}")
    print("Make sure osu! and tosu are running, then pick a map in song select.")
    print("Press Ctrl+C to stop.\n")
    if args.browser:
        webbrowser.open(address)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
