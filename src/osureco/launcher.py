#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher.py - one-click entry point.

Starts the local server and opens the browser.

By default the search runs on the hosted API: nothing to download, and the
first launch takes seconds instead of minutes. `--mode local` searches on this
machine instead, which needs the index -- ~150 MB derived from the public ppy
beatmap dump, fetched prebuilt from the repository's GitHub releases, or built
here with `--build`.

If the API cannot be reached and no index is installed, the launcher falls back
to downloading one rather than opening a page that could not answer anything.

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


def asset_name(variant: str = "nomod") -> str:
    return f"osureco-index-{variant}.zip"


def index_url(variant: str = "nomod") -> str:
    """
    OSURECO_INDEX_URL peut etre soit une URL complete (une seule variante),
    soit un dossier se terminant par "/" sous lequel les archives portent leur
    nom habituel -- c'est ce qui permet a un serveur de tirer plusieurs
    variantes depuis un meme miroir.
    """
    override = os.environ.get("OSURECO_INDEX_URL")
    if override:
        return override + asset_name(variant) if override.endswith("/") else override
    slug = remote_slug() or UPSTREAM_SLUG
    return (f"https://github.com/{slug}/releases/latest/download/"
            f"{asset_name(variant)}")


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


def download_index(url: str, variant: str = "nomod") -> bool:
    print(f"Downloading the prebuilt {variant} index from:\n  {url}")
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

    if not has_index(variant):
        print("  the archive did not contain a usable index.")
        return False
    print(f"Index ready in {target}")
    return True


def ensure_index(variants: list[str] | tuple[str, ...] = ("nomod",)) -> bool:
    """
    Garantit la presence des variantes demandees, en telechargeant les absentes.

    Utilise au demarrage d'un serveur : une machine neuve doit pouvoir servir
    l'API sans qu'on y copie 300 Mo a la main.
    """
    ok = True
    for variant in variants:
        if has_index(variant):
            print(f"  index {variant}: already installed")
            continue
        url = index_url(variant)
        if not download_index(url, variant):
            print(f"  index {variant}: unavailable at {url}")
            ok = False
    return ok


def probe_api(url: str) -> tuple[bool, str]:
    """Le serveur repond-il, et avec quoi ? N'echoue jamais."""
    from .remote import RemoteBackend

    backend = RemoteBackend(url)
    if not backend.probe():
        return False, backend.last_error
    info = backend.info()
    total = info.n_maps.get("nomod") or (max(info.n_maps.values()) if info.n_maps else 0)
    return True, f"{total} maps, corpora: {', '.join(info.variants)}"


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
    parser.add_argument("--mode", choices=("remote", "local"), default=None,
                        help="where the search runs (default: remote)")
    parser.add_argument("--api", default=None, help="hosted API to use")
    parser.add_argument("--no-browser", dest="browser", action="store_false")
    args = parser.parse_args(argv)

    from .remote import DEFAULT_API_URL

    mode = args.mode or os.environ.get("OSURECO_MODE") or "remote"
    # `--api ""` ou `OSURECO_API_URL=` desactive le distant : distinguer la
    # valeur vide de l'absence de valeur est ce qui rend le refus possible
    if args.api is not None:
        api_url = args.api.strip()
    else:
        api_url = os.environ.get("OSURECO_API_URL", DEFAULT_API_URL).strip()
    if not api_url:
        mode = "local"

    print("osu!MapMatcher")
    print(f"  data folder : {data_dir()}")

    if args.build:
        if not build_index(args.variants):
            return 1
        mode = "local"

    if mode == "remote":
        reachable, detail = probe_api(api_url)
        print(f"  search      : {api_url}")
        print(f"                {detail}" if reachable
              else f"                unreachable - {detail}")
        if not reachable and not has_index():
            # ni serveur ni index : autant installer l'index maintenant plutot
            # que d'ouvrir une page qui ne saura rien repondre
            print("\nThe server is unreachable and there is no local index yet.")
            if not fetch_or_explain():
                return 1
            mode = "local"
        elif not reachable:
            print("                falling back to the local index")
            mode = "local"
    elif not has_index():
        print("\nNo index found yet - this is a one-time setup.")
        if not fetch_or_explain():
            return 1

    if mode == "local":
        print(f"  index       : local, in {index_root()}")

    # imported late so a missing index fails with our message, not a stack trace
    import uvicorn

    from .server import create_app

    address = f"http://{args.host}:{args.port}"
    print(f"\nServer: {address}")
    print("Make sure osu! and tosu are running, then pick a map in song select.")
    print("Press Ctrl+C to stop.\n")
    if args.browser:
        webbrowser.open(address)
    uvicorn.run(
        create_app(api_url=api_url, mode=mode),
        host=args.host, port=args.port, log_level="warning",
    )
    return 0


def fetch_or_explain() -> bool:
    """Telecharge l'index prebati, ou explique comment le construire."""
    url = index_url()
    if url and download_index(url):
        return True
    if not url:
        print("\nCould not work out where to download the index from "
              "(no GitHub remote in .git/config).")
    print(
        "\nFall back to building it yourself:\n"
        "  1. grab a *_osu_files.tar.bz2 dump from https://data.ppy.sh\n"
        f"  2. extract it into {data_dir() / 'osu_files'}\n"
        "  3. run this again with --build\n"
    )
    return False


if __name__ == "__main__":
    raise SystemExit(main())
