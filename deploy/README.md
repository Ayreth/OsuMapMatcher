# Deploying the osu!MapMatcher API

Everything needed to run the recommendation engine as a service someone else's
client can call. The HTTP contract itself is documented separately, in
[`docs/API.md`](../docs/API.md).

**Read this first.** The desktop tool does not need this server. It runs
entirely on the player's machine, and that stays the default and the fallback.
A hosted API exists for people who cannot install Python or download 164 MB,
and for clients that physically cannot host a backend (a tosu counter is a
static HTML page). If your instance goes down, every client must keep working
against its local index — that is not an implementation detail, it is the
failure the original OsuMapMatcher died of.

---

## Contents

- [What you are running](#what-you-are-running)
- [Sizing](#sizing)
- [Step 1 — get an index onto the machine](#step-1--get-an-index-onto-the-machine)
- [Step 2a — Docker Compose](#step-2a--docker-compose-recommended)
- [Step 2b — systemd and a venv](#step-2b--systemd-and-a-venv)
- [Step 2c — just run it](#step-2c--just-run-it-for-a-test)
- [Step 3 — TLS and a reverse proxy](#step-3--tls-and-a-reverse-proxy)
- [Step 4 — verify](#step-4--verify)
- [Environment variables](#environment-variables)
- [Operating it](#operating-it)
- [Sharp edges](#sharp-edges)

---

## What you are running

One stateless Python process. It memory-maps a prebuilt index and answers
searches; it has no database, no session, no writes, and no outbound calls
except the optional index download at first boot.

```
client ──HTTP──▶ reverse proxy ──▶ uvicorn ──▶ osumapmatcher.api
                    (TLS)                        │
                                                 ├── data/index/nomod/  (mmap)
                                                 ├── data/index/DT/     (mmap)
                                                 └── …
```

The local desktop server (`osumapmatcher.launcher`) and this API mount **the same
router**, so both speak the same `/api/v1` protocol. A client can point at
either one without changing a line — which is what makes remote-to-local
fallback cheap.

---

## Sizing

Measured on a 24-core dev machine, one uvicorn worker, `nomod` corpus of
151,558 maps:

| | |
|---|---|
| Search by checksum | median **10 ms**, p95 32 ms |
| Upload + featurise a map absent from the corpus | median **18 ms** |
| Throughput, 16 concurrent clients | **~134 searches/s** |
| Throughput, 8 concurrent uploads | **~101 req/s** |
| Working set, one variant warm | **~180 MB** |
| Cold start (loading one variant) | ~0.4 s |

What that means in practice:

- **A 1 vCPU / 1 GB VPS is enough** for a small community. Searching is
  milliseconds of numpy; the corpus-hash shortcut means uploads of ranked maps
  cost nothing either.
- **Disk is the real constraint**: ~290 MB per mod variant (59 MB vectors +
  59 MB raw + 81 MB ANN index + 33 MB SQLite). All five variants ≈ 1.5 GB.
- **Scale with `--workers`, not with threads.** Each worker is a process with
  its own memory-mapped view; the OS shares the pages. Two workers on two cores
  roughly doubles throughput.
- RAM grows with the *touched* pages of the mmapped vectors, so a busy instance
  drifts up toward the index size. Budget 1 GB per variant you actually serve
  if you want everything comfortably cached.

---

## Step 1 — get an index onto the machine

The API refuses to search without one (`/health` reports `ready: false`, search
endpoints answer 503). Three ways, pick one.

### Download the prebuilt release asset

```bash
python scripts/fetch_index.py --variants nomod
python scripts/fetch_index.py --check --variants nomod,DT     # what is installed?
```

It resolves the URL the same way the desktop launcher does: `OSUMAPMATCHER_INDEX_URL`
first, then this clone's git remote, then upstream. Only `nomod` is published
upstream; other variants have to come from your own mirror or your own build.

To serve several variants from your own storage, put the archives behind one
prefix and point the whole folder at it:

```bash
export OSUMAPMATCHER_INDEX_URL="https://cdn.example.com/osu-mapmatcher/"
# fetches osu-mapmatcher-index-nomod.zip and osu-mapmatcher-index-DT.zip from that prefix
python scripts/fetch_index.py --variants nomod,DT
```

### Build it elsewhere and copy it up

Building needs the ~7 GB ppy dump and a few minutes of many-core CPU — usually
not something you want to do on a small VPS.

```bash
# on a machine that has the dump
python scripts/build_all.py --variants nomod,DT
python scripts/pack_release.py --variant nomod       # -> data/release/*.zip

# then, on the server
rsync -avP data/index/ server:/var/lib/osu-mapmatcher/index/
```

The layout the server expects:

```
$OSUMAPMATCHER_DATA/index/
  nomod/{scaler.npz,vectors.npy,vectors_raw.npy,index.usearch,corpus.sqlite}
  DT/…
```

### Ship the corpus and rebuild on the server

Copying finished indexes up means moving ~235 MB per variant every time. The
corpus they are built from is far smaller — features, not files — and the
server has the CPU to turn one into the other in about 20 s per variant. Two
scripts do that end to end:

```bash
# from the build machine: pushes the corpus, then rebuilds and swaps remotely
python scripts/publish_index.py --dry-run
python scripts/publish_index.py
```

```bash
# on the server, if you would rather drive it yourself
./deploy/update-index.sh              # every variant in deploy/.env
DRY_RUN=1 ./deploy/update-index.sh    # check the inputs, build nothing
```

Only `data/corpus/<variant>_unranked/` travels by default (~2.2 kB per map).
The dump corpora are ~360 MB and only change when ppy publishes a new dump, so
they need `--full`.

`update-index.sh` builds into `data/index_new` and only swaps once the API
answers `"ready":true`, rolling back to the previous index if it does not. It
also passes what a hand-typed `build_index.py` invocation usually omits:

- **both corpora** — `--corpus data/corpus/nomod` alone yields an index without
  a single unranked map. Nothing warns you; the index is perfectly valid and
  simply missing its whole pool.
- **`--manifest` and `--loved`** — without them the `status` and `playcount`
  columns do not exist, graveyard maps come back labelled as dump maps, and the
  API reports them as ranked.

### Let the container fetch it on first boot

The default Docker command passes `--fetch --variants nomod`, which downloads
whatever is missing into the volume before serving. Fine for a first
deployment; drop the flag once the volume is populated so a restart is instant.

---

## Step 2a — Docker Compose (recommended)

From the repository root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

The image contains code only. The index lives in the named volume
`osu-mapmatcher-index`, so rebuilding the image never re-downloads 290 MB.

The port is published on `127.0.0.1:8080` on purpose: put a reverse proxy in
front of it rather than exposing uvicorn directly.

Edit the `environment:` block for your own settings — at minimum, review
`OSUMAPMATCHER_API_CORS` and `OSUMAPMATCHER_API_RATE`. Uncomment the `--workers` lines to
use more than one core.

To add a variant later, download it into the volume of the running container,
then restart so the new corpus is picked up:

```bash
COMPOSE="docker compose -f deploy/docker-compose.yml"
$COMPOSE exec api python -c "from osumapmatcher.launcher import ensure_index; ensure_index(['DT'])"
# then add DT to OSUMAPMATCHER_API_VARIANTS and to the --variants flag, and:
$COMPOSE up -d
```

## Step 2b — systemd and a venv

For a plain VM without Docker. The unit file is
[`osu-mapmatcher-api.service`](osu-mapmatcher-api.service); its header lists the exact
commands. In short:

```bash
sudo useradd --system --home /var/lib/osu-mapmatcher --create-home osu-mapmatcher
sudo git clone https://github.com/Ayreth/OsuMapMatcher /opt/osu-mapmatcher
sudo python3 -m venv /opt/osu-mapmatcher/.venv
sudo /opt/osu-mapmatcher/.venv/bin/pip install -e "/opt/osu-mapmatcher[api]"

sudo -u osu-mapmatcher OSUMAPMATCHER_DATA=/var/lib/osu-mapmatcher \
     /opt/osu-mapmatcher/.venv/bin/python /opt/osu-mapmatcher/scripts/fetch_index.py

sudo cp /opt/osu-mapmatcher/deploy/osu-mapmatcher-api.service /etc/systemd/system/
sudo mkdir -p /etc/osu-mapmatcher && sudo touch /etc/osu-mapmatcher/api.env   # secrets here
sudo systemctl daemon-reload && sudo systemctl enable --now osu-mapmatcher-api
sudo systemctl status osu-mapmatcher-api
```

The unit is hardened (`ProtectSystem=strict`, no write access outside
`/var/lib/osu-mapmatcher`, restricted address families). It needs no privileges: the
service only reads files and answers HTTP.

Put the token and any host-specific overrides in `/etc/osu-mapmatcher/api.env`, not in
the unit — that file is `EnvironmentFile=-`, so it may be absent:

```ini
OSUMAPMATCHER_API_TOKEN=a-long-random-string
OSUMAPMATCHER_API_RATE=240
```

## Step 2c — just run it (for a test)

```bash
pip install -e ".[api]"
python -m osumapmatcher.api --host 127.0.0.1 --port 8080 --fetch
```

Useful flags: `--workers N`, `--variants nomod,DT`, `--index /path/to/index`.
`GET /docs` serves a live OpenAPI console — handy while wiring a client, worth
turning off in production with `OSUMAPMATCHER_API_DOCS=0`.

---

## Step 3 — TLS and a reverse proxy

Never expose uvicorn directly. Two examples ship here:

- [`Caddyfile`](Caddyfile) — certificates and HTTP/2 with no configuration.
- [`nginx.conf`](nginx.conf) — if nginx and certbot are already in place.

Whatever you use, three things matter:

1. **Forward the client IP** (`X-Forwarded-For`) and set
   `OSUMAPMATCHER_API_TRUST_PROXY=1`. Without both, every client shares the proxy's
   token bucket and one heavy user throttles everybody.
2. **Cap the request body** at the proxy (4 MB), so oversized uploads are
   refused before they are buffered.
3. **Point health checks at `/health`** and keep it out of the access log.

A browser client — a tosu counter served from `http://127.0.0.1:24050` — needs
CORS. The default `OSUMAPMATCHER_API_CORS=*` allows it; narrow it only if the API is
meant to serve one site.

---

## Step 4 — verify

Run these against the deployed URL. Any failure here is worth fixing before
telling anyone the API exists.

```bash
API=https://api.example.com

# 1. up, and an index is loaded
curl -s $API/health

# 2. feature version and corpus size — write the version down
curl -s $API/api/v1/meta | head -c 300

# 3. a real search on a known map
curl -s "$API/api/v1/similar?checksum=9af49bce8882fa219a24aa815a03e698&k=3"

# 4. the zero-dependency path
curl -s -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @some-map.osu "$API/api/v1/similar/file?k=3"

# 5. CORS, as a browser client would see it
curl -si -H "Origin: http://127.0.0.1:24050" $API/api/v1/meta | grep -i access-control

# 6. the rate limiter actually limits -- in parallel on purpose: a sequential
#    curl loop is slower than the bucket refills, so it never trips
seq 1 200 | xargs -P 8 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  "$API/api/v1/similar?checksum=9af49bce8882fa219a24aa815a03e698" | sort | uniq -c
```

Expected: `ready: true`, a `feature_version`, three matches, `source: "corpus"`
on the upload (the file's MD5 was found in the corpus, so nothing was
featurised), an `access-control-allow-origin` header, and a mix of `200` and
`429` on the last one.

---

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `OSUMAPMATCHER_DATA` | `<repo>/data` | data folder; the index is read from `$OSUMAPMATCHER_DATA/index` |
| `OSUMAPMATCHER_INDEX_URL` | GitHub release | where `--fetch` downloads from; a full URL, or a prefix ending in `/` |
| `OSUMAPMATCHER_API_VARIANTS` | all present | restrict which corpora are served, e.g. `nomod,DT` |
| `OSUMAPMATCHER_API_CORS` | `*` | allowed origins, comma-separated |
| `OSUMAPMATCHER_API_TOKEN` | empty | if set, `Authorization: Bearer` is required (except `/health`, `/meta`) |
| `OSUMAPMATCHER_API_RATE` | `120` | requests per minute per IP; `0` disables |
| `OSUMAPMATCHER_API_BURST` | `60` | bucket size, i.e. how much burst is tolerated |
| `OSUMAPMATCHER_API_FILE_COST` | `5` | how many tokens an upload costs versus 1 for a search |
| `OSUMAPMATCHER_API_MAX_UPLOAD` | `4000000` | largest accepted `.osu`, in bytes |
| `OSUMAPMATCHER_API_CONCURRENCY` | CPU count | simultaneous CPU-bound jobs per worker |
| `OSUMAPMATCHER_API_TRUST_PROXY` | `0` | read the client IP from `X-Forwarded-For` |
| `OSUMAPMATCHER_API_CACHE` | `256` | featurisations kept in memory, keyed by content hash |
| `OSUMAPMATCHER_API_DOCS` | `1` | `0` hides `/docs` and `/openapi.json` |

---

## Operating it

**Logs.** uvicorn on stdout; `docker compose logs -f` or `journalctl -u
osu-mapmatcher-api -f`. Startup logs the variants served; a missing index logs an
error and keeps serving `/health` so you can see it.

**Counters.** `GET /api/v1/stats` gives requests, searches, featurisations,
cache hits, throttled and unauthorised counts since startup. The number to
watch is `featurized` versus `searches`: if uploads are dominating, either your
users are on unranked maps or a client is not sending checksums first.

**Updating the code.** `git pull`, then rebuild the image (or
`systemctl restart osu-mapmatcher-api`). The index is untouched — unless
`feature_version` changed, in which case it must be rebuilt too, and every
client must be updated with it. `/api/v1/meta` tells you which version is live.

**Updating the index.** Fetch or copy the new variant folder alongside the old
one, then restart. Restarting is required: the index is memory-mapped and read
once at startup. There is no hot reload, and swapping files under a running
process is a good way to serve corrupted vectors.

**Backups.** None needed. Everything on disk is regenerable from the public
ppy dump.

---

## Sharp edges

**The feature version is load-bearing.** A client that computes vectors with a
different feature set gets meaningless-but-plausible results. That is why the
API answers 409 rather than searching. If you rebuild your index from a
modified `FEATURE_SPEC`, you have forked the protocol: publish the new version
string, and expect old clients to fall back to local.

**Rate limits are per worker.** Each uvicorn worker keeps its own token bucket
in memory. With `--workers 2` and `OSUMAPMATCHER_API_RATE=120`, a client that lands
on alternating workers can reach 240/min. Halve the setting, or put the real
limit in the reverse proxy, which sees every request.

**No shared state at all.** No Redis, no database — deliberately. It keeps the
thing operable by one person. The cost is exactly the point above.

**`--fetch` on every boot is a network dependency.** Convenient for the first
deployment, wrong for steady state: if GitHub is unreachable and the volume was
wiped, the container starts without an index. Once the volume is populated,
drop the flag.

**Watch the disk before adding variants.** Five variants are ~1.5 GB. The full
cross product discussed in the handoff (18 variants) is ~5 GB, and the API can
only serve what has actually been built.

**Uploaded beatmaps transit your server.** They are held in memory, featurised,
and dropped; nothing is written to disk. Say so if you publish the API, and do
not add request-body logging to a proxy in front of it.

**A public instance is a promise.** The desktop tool's whole point is that it
keeps working offline. If you publish a URL that clients start depending on,
keep the local path first-class in whatever client you ship.
