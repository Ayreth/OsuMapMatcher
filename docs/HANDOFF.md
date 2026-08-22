# Handoff

State of the project, the things worth not rediscovering, and the two pieces of
work queued next. Written to be enough for a cold start — a new contributor or a
fresh assistant session should be able to work from this file alone.

Last updated: 2026-08-22, after the hosted API landed (previous revision: commit `4113aa9`).

---

## 1. Where things stand

**Shipped and working.** <https://github.com/Ayreth/OsuMapMatcher>, public, MIT,
release `v0.1.0`.

You pick a map in osu! song select; tosu reports it; the tool featurises it and
returns the structurally closest maps out of 151,558, live, in a browser page.

| | |
|---|---|
| Corpus | 151,558 osu!standard maps per mod variant, from the `2026_08_01` ppy dump |
| Variants built | `nomod`, `DT`, `HR`, `DTHR`, `HT` |
| Feature vector | 96 columns in 5 groups — aim 27, speed 14, rhythm 24, spacing 17, parameters 14 |
| Search | ~10 ms (was 250 ms on dense star bands, see §5) |
| Tests | 99, all passing |
| Distribution | prebuilt `nomod` index as a GitHub release asset, 164 MB |
| Hosted API | live at https://api.ayreth.site, five mod corpora |
| Default mode | remote: a fresh install downloads no index at all |
| Clients | the desktop page, and a tosu counter — the same file, two hosts |
| On disk here | `data/corpus` 359 MB, `data/index` 1.1 GB |

Everything under `data/` is generated and gitignored. The repo itself is ~1 MB.

---

## 2. Running and rebuilding

There is **no system Python** on this machine — `python`, `py` and `python3` all
resolve to the Microsoft Store stub and fail. Always use the project venv:

```
.venv\Scripts\python.exe
```

| Task | Command |
|---|---|
| Run the app | `.venv\Scripts\python.exe -m osumapmatcher` (or double-click `start.bat`) |
| Tests | `.venv\Scripts\python.exe -m pytest tests -q` |
| Rebuild every variant | `.venv\Scripts\python.exe scripts\build_all.py` |
| Rebuild one variant | `scripts\build_all.py --variants DT` |
| Quality check on known maps | `scripts\sanity_check.py --why` |
| Feature sanity on a sample | `scripts\featurize_sample.py --n 3000` |
| Pack a release asset | `scripts\pack_release.py --variant nomod` |
| Run the hosted API | `.venv\Scripts\python.exe -m osumapmatcher.api --port 8080` |
| Install an index for a server | `scripts\fetch_index.py --variants nomod,DT` |
| See what is installed | `scripts\fetch_index.py --check --variants nomod,DT` |
| Phase-1 tosu diagnostic | `scripts\sync_probe.py` |

A full rebuild is ~2 minutes of featurisation per variant on 24 cores, plus ~12 s
of indexing. `build_all.py` is interruptible and resumes per 50k-map chunk.

The dump lives at `2026_08_01_osu_files/` (three nested folders of the same
name — `paths.find_dump()` walks down and picks the level with the most `.osu`
files, because the intermediate levels hold a handful of stragglers that would
otherwise look like a valid corpus of eleven maps).

---

## 3. Module map

```
src/osumapmatcher/
  features.py   the core: featurize(), FEATURE_SPEC, Mods, .osu parser      775 l
  index.py      Scaler, Searcher, SearcherSet, search, explain()            693 l
  api.py        the hosted API: /api/v1, quota, token, CORS, upload      1080 l
  remote.py     client of the hosted API, urllib only, no new deps          297 l
  server.py     FastAPI: /api/similar, /api/current, /events (SSE), /       288 l
  corpus.py     batch build, multiprocessing, chunked parquet, resume       280 l
  tosu.py       websocket client, debounce, live featurisation             249 l
  launcher.py   one-click bootstrap: fetch index, start server              200 l
  paths.py      every path resolution, no hardcoded locations               118 l
  static/index.html   the whole UI, vanilla, one file, two hosts            947 l
```

Data flow, live: `tosu websocket → debounce 300 ms → GET /files/beatmap/file →
featurize() → scaler → weighted distance over pre-filtered candidates → JSON → page`.

Data flow, batch: `dump → ProcessPoolExecutor → featurize() → parquet chunks →
RobustScaler → vectors.npy + vectors_raw.npy + usearch index + SQLite`.

Data flow, remote: `tosu → local server → checksum → GET /api/v1/similar on
api.ayreth.site → JSON → page`. The local server is the one that talks to the
API, not the browser: it already holds the checksum, the mods and the .osu, and
it keeps CORS out of the picture. `server.py` and
`api.py` mount the **same router** (`build_router`), so the local host and a
hosted one speak one protocol — that is what makes a client's remote → local
fallback a change of base URL and nothing else.

---

## 4. Invariants — do not break these

**`featurize()` is one function, used identically for the corpus and the live
map.** If the two ever diverge, every distance in the product becomes
meaningless and nothing will look obviously broken. There is no "batch" branch
and no "live" branch. Keep it that way.

**The vector order comes from `FEATURE_NAMES`, never from dict insertion
order.** `feature_vector()` reads by name and raises on a missing key.
`test_ordre_independant_de_l_ordre_d_insertion` rebuilds the dict reversed and
asserts the vector is identical. This is the single most dangerous class of bug
in the project.

**Adding, removing or reordering a feature invalidates every index on disk.**
The scaler stores `feature_names` and `Scaler.load()` refuses to load a
mismatched one. After any change to `FEATURE_SPEC`, rebuild all variants.

**No mods must stay byte-identical.** `Mods.rosu_kwargs()` passes nothing at all
when there are no mods, so the nomod path is exactly what built the corpus.
`test_sans_mods_le_vecteur_est_inchange` guards this.

**The live map is featurised with the *canonical* variant mods, not the raw
ones.** With `HDDT` active, `variant_for_mods` gives `DT` and the watcher
featurises with `"DT"` — because the DT corpus was built with `"DT"`. Featurising
the query with `HDDT` against a `DT` corpus would introduce a silent bias.

**The local server and the hosted API mount the same router.** `build_router()`
in `api.py` is included by both; the only difference is the `ApiConfig` handed
to it (`ApiConfig.local()` has no quota and no token). Adding an endpoint to one
and not the other would break the property that a client can switch hosts by
changing a base URL, which is the whole point of the remote mode.

**`stars` is a filter, not a feature.** It correlates 0.99 with `d_aim` and is
near-constant inside a star band. It lives in `META_NAMES`.

---

## 5. Findings already paid for

These were measured on real data. Re-deriving them costs hours.

**rosu-pp-py 4.0.2, osu!standard.** `rhythm`, `reading`, `consistency_factor`,
`mechanical_difficulty`, `mono_stamina_factor`, `n_objects` and `preempt` are all
`None`. Populated strain series: `aim`, `aim_no_sliders`, `speed`, `flashlight`.
The rhythm group is therefore *reconstructed* from BPM, delta-times and the
flashlight series — there is no rhythm attribute to read.

**One corpus per mod combination is not optional.** Querying a DT-featurised map
against the nomod corpus looks for maps worth 12★ *without* mods, which barely
exist. Measured on `BLUE ENCOUNT - HOPE [SURVIVOR]`, best-match distance:

| | against nomod corpus | against matching corpus |
|---|---|---|
| nomod | 1.57 | 1.57 |
| HR | 1.90 | 1.57 |
| DT | 2.99 | **1.47** |
| DTHR | 6.98 | **1.48** |

**HD moves the flashlight strains; FL does not.** Measured across several maps:
HD raises `flashlight` strain mean and p99 by 28–36%, FL leaves the series
untouched (it only changes the scalar `flashlight` attribute and stars, neither
of which is a feature). The README used to claim the opposite. Since HD is
canonicalised away on both sides, this is currently consistent but unmodelled.

**`RobustScaler` defaults are a trap here.** With `quantile_range=(25,75)`, four
corpus columns have a zero IQR; sklearn then substitutes a scale of 1 and those
columns stay in raw units among scaled ones — exactly the failure that made the
original OsuMapMatcher bad at tech maps. `(5,95)` leaves zero dead columns.
Values are clipped to ±5 afterwards because the corpus contains maps up to 193★.

**13 features were removed after measuring redundancy** (r > 0.99):
`great_hit_window` (= 80 − 6·od), `ratio_circles`, `total_time_s`, `meter`, and
`*_mean` / `*_std` on all four strain series. 13 pairs above r = 0.985 remain,
all intra-series or aim↔aimns; leave them unless sanity checks look wrong, and
if they do, the first lever is dropping the absolute `aimns_*` quantiles.

**Small data landmines, all fixed and regression-tested.** One map has a
denormal `beatLength` producing an infinite BPM. One in seven dump files has
`BeatmapID:0` while the filename carries the id (recovered in `corpus.py`, not in
`featurize()` — an id of 0 is real information for an unsubmitted live map). A
very slow Easy had every object pair beyond the fixed 1 s continuity threshold,
zeroing its whole geometry — the threshold is now `max(1000 ms, 4 × median gap)`.

**`song_key()` deduplicates on title only.** The artist field is too unstable to
group a song (`Aiurabu` vs `Aiurabu (CV: Nakajima Yui, …)`).

**`vectors.npy` is column-major, and that broke the ANN path.**
`DataFrame.to_numpy()` returns a Fortran-ordered array, so `np.save` wrote one,
so a single row read back from the memory-mapped file is *not* contiguous — and
usearch refuses a non-contiguous query with `Matrix rows must be contiguous`.
It only fired when the pre-filter left more than `EXACT_LIMIT` (30,000)
candidates, i.e. on any mid-star map, which is most of the corpus; the maps used
for sanity checks were all 5–12★ and sat below the threshold. Fixed in three
places: `Scaler.transform` now returns C-order (new builds), `target_from_checksum`
copies contiguously, and `search` guards the usearch call (indexes already
distributed). `tests/test_search.py` writes its fixture in Fortran order on
purpose.

**One sqlite connection, several threads, and a 500 nobody could explain.**
`Searcher.db` is opened with `check_same_thread=False` and was shared without a
lock. Two overlapping searches steal each other's cursor and `row["pos"]` ends
up raising `IndexError: tuple index out of range` — served to the browser as a
bare Internal Server Error. It only shows when requests overlap, i.e. exactly
when a player scrolls fast through song select. The lock now lives inside
`Searcher`, not at the call sites: there were three of them and it takes one
that forgets. `has_checksum()` exists so no caller has to touch `db` at all.

**A map absent from the corpus is not an outage.** `RemoteError` classified
every status-0 error as transient, including "this map is not in the server's
corpus" — so hovering one unsubmitted map put the server in a 20-second
quarantine and dropped everyone to local. `transient` is now explicit where the
heuristic is wrong.

**The SQL pre-filter cost 250 ms, twenty-five times the search itself.**
`idx_maps_stars` finds the rows by star rating quickly, but testing `length_s`,
`beatmapset_id` and `checksum` sends SQLite to fetch 30,000 rows one by one.
The same conditions over three numpy columns held in memory (3.6 MB per variant,
loaded once at startup) take under a millisecond. `_candidates` is now numpy;
`tests/test_search.py` compares it against the old SQL on every map of its
corpus rather than trusting the reading. Total search: 250 ms → ~4 ms.

---

## 6. Next: finish mod coverage

### What already exists

`Mods.parse()` handles clock rate (DT/NC 1.5, HT/DC 0.75, plus a free `rate` for
lazer) and CS multipliers (HR ×1.3, EZ ×0.5, capped at 10). AR, OD and HP come
back mod-aware from rosu — OD is derived from `great_hit_window` precisely so it
follows the mods. So **EZ already featurises correctly**; it simply has no corpus
and `variant_for_mods` sends it to `nomod`.

### What is missing

1. **EZ** — the cheapest win. Add `"EZ"` to `VARIANTS`, build, done.
2. **Missing rate × difficulty combos** — `HTHR`, `DTEZ`, `HTEZ`.
3. **FL** — needs a decision, not just a build. FL does not change any series we
   use, so an FL corpus would differ from nomod *only* through `stars`, which is
   a filter and not a feature. A dedicated corpus is probably not worth 290 MB;
   the better fix may be to adjust the star band for FL rather than build.
4. **HD** — genuinely shifts 10 of the 96 columns. Options: build HD variants
   (doubles the count), keep ignoring it (current behaviour, consistent), or drop
   the `fl_*` columns and rebuild the rhythm group without them. Measure before
   choosing: run `sanity_check.py` on an HD-featurised query against the nomod
   corpus and see whether the top 10 actually degrades.
5. **Custom rates (lazer)** — `Mods.parse` accepts any rate, but
   `variant_for_mods` snaps to DT/HT/nomod. A 1.2× rate currently falls back to
   nomod, which is wrong. Either build rate buckets or featurise the corpus on
   demand for that rate (expensive).

### Cost per variant

~2.2 minutes of build, ~290 MB on disk (59 MB vectors + 59 MB raw + 81 MB ANN +
33 MB SQLite, before compression). The full cross product of
{nomod, DT, HT} × {nomod, HR, EZ} × {nomod, HD} is 18 variants — roughly 40
minutes and 5 GB. Pick a subset by what people actually play.

### How to verify

The strongest check is **cross-variant stability**: the same neighbours should
recur across variants, because a mod transforms every map the same way. On
`HOPE [SURVIVOR]`, the top 3 are the same maps in nomod, HR, DT, DTHR and HT,
with the ranking only shuffling. If a new variant returns a completely different
neighbourhood, something is wrong with it. `scripts/sanity_check.py --variant EZ`
is the tool.

---

## 7. Hosted API and remote mode — both shipped

### What exists now

`src/osumapmatcher/api.py` serves `/api/v1` and is deployable
(`deploy/`: Dockerfile, compose, systemd unit, Caddy and nginx examples;
`deploy/README.md` for sizing and operations, `docs/API.md` for the contract).

The design question from the previous handoff — featurise on the client or on
the server — was answered with **both**, because the answer differs per client
and the two paths cost almost the same to support:

  1. `checksum` → the vector is already on disk, zero CPU;
  2. `features` (a dict keyed by name) → the client did the work;
  3. the raw `.osu` → the server featurises, ~20 ms.

The decision that made option B cheap: **a map's checksum is the MD5 of its
`.osu`**, so `/similar/file` hashes the body and looks it up in the corpus
before doing anything. Every ranked map costs nothing to "featurise". Only
unsubmitted or edited maps hit the CPU. A dependency-free HTML/JS client can
therefore always post the file and still be nearly free to serve — which is
what a tosu counter needs.

Both paths end in `target_from_features`, the same function the live desktop
path uses. There is no second implementation of anything.

Measured, one uvicorn worker, 24-core machine: 134 searches/s at 16 concurrent
clients, 101 uploads/s at 8; ~180 MB resident with `nomod` warm.

Guards, all env-tunable: CORS, optional bearer token, per-IP token bucket
(uploads cost 5× a search), 4 MB body cap, CPU semaphore, LRU on featurisation.
And the one that matters: **`feature_version`**, a hash of `FEATURE_NAMES`
returned by every response and rejected with 409 on mismatch — a vector built
from a different feature set produces plausible, wrong answers, silently.

### The client half, shipped

The desktop tool defaults to `remote` and talks to https://api.ayreth.site.
A fresh install now downloads **nothing**: no index, no 164 MB, first launch in
seconds. `remote.py` is the client, `urllib` only — adding a dependency to the
one thing meant to be light would have been self-defeating.

Three attempts, in order of cost, and it only moves up a rung when the previous
one *cannot* answer, never because it failed for another reason:

  1. the checksum. Covers essentially all real traffic;
  2. the features computed here, if the server announces the same
     `feature_version`. ~3 KB, and the player's file stays on their disk;
  3. the raw `.osu`, when the versions differ. The server featurises with its
     own code, so an old client against a newer server still gets correct
     answers instead of a 409. This is what makes updating the server safe.

Fallback is automatic and *said out loud*: a transient failure switches to the
local index, the header shows `local — server unavailable (unreachable)`, and a
20 s cooldown stops every map change from re-probing a server that is down.
With no local index the failure is an honest 502 rather than a silent nothing.

`--api ""` (or `OSUMAPMATCHER_API_URL=`) disables the network for good. An empty
string and an absent value have to mean different things, otherwise there is no
way to say "never phone home" — that distinction is load-bearing, not a detail.

### The tosu counter, shipped

`counter/osu-mapmatcher/` is a folder to drop into tosu's `static/`. No
Python, no launcher, no index: the page reads tosu's websocket directly and
searches on the API.

It is **the same file** as the desktop page. `static/index.html` detects its
host — a local server answers `/api/current` with a `backend` block, tosu does
not — and switches between two small adapters. `scripts/pack_counter.py`
generates the counter folder from it, and `tests/test_counter.py` fails if the
shipped copy drifts. Two interfaces to keep in sync was the obvious trap here.

The CORS question that made the desktop tool proxy does not arise: tosu serves
the counter, so `/files/beatmap/file` is same-origin, and the API allows any
origin. Measured end to end on a real session: 178 ms for the upload path,
including fetching the file from tosu.

### What is still open

**The counter has no settings UI.** tosu counters can declare settings that
the dashboard renders; ours takes its API address from `?api=` or from the
build. Fine for one instance, thin if it is ever published.

**`start.bat` still installs the heavy extras** (`corpus,index`: pandas,
pyarrow, scikit-learn). In remote mode none of them are used. Splitting the
install would cut the first run from a couple of minutes to about twenty
seconds, at the cost of making local mode a second, explicit install step.

**Rate limits are per worker** (each process has its own bucket). Fine for one
worker; with several, either halve the setting or move the limit to the reverse
proxy.

---

## 8. Backlog

- **Index the local `Songs` folder** so graveyard and unranked maps become
  recommendable, and so results can be filtered to "maps I already have" or
  "maps I don't". Explicitly deferred once already.
- **Shrink the release asset** from 164 MB to ~110 MB by dropping the usearch
  index: exact scoring over 151k × 96 mmap'd floats is ~50 ms, so the ANN only
  earns its place on unfiltered queries. `Searcher` would need to tolerate a
  missing `index.usearch`.
- **Exclude the query's own song** from results. Currently the same song from a
  different mapset can rank first — arguably a good validation signal, arguably
  noise.
- **Slider shapes** are ignored entirely (v1 decision): no curve types, no
  repeats, no SV.
- **Rebuild the indexes** so `vectors.npy` lands in C order. Purely a
  performance matter now that reading handles both — `scripts/build_index.py`
  from the existing parquet corpus, no re-featurisation needed. Measured gain
  on a 31k-row gather: 13 ms → 6 ms.
- **`EXACT_LIMIT` may no longer earn its place.** The ANN detour exists because
  scoring 30,000 candidates was slow; now that the pre-filter is numpy, exact
  scoring of the whole band is a few milliseconds. Dropping the ANN path would
  also remove `index.usearch` (81 MB) from the release asset.

---

## 9. Environment notes

- Python 3.12.10, installed via `winget`, at
  `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. The project venv is at
  the repo root.
- `gh` 2.98 is installed as a winget **portable** package — not on `PATH` and not
  under `Programs`. It lives at
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.cli_*\bin\gh.exe`.
- Git identity for this repo is **Ayreth**
  `<34312396+Ayreth@users.noreply.github.com>`, set locally. The Windows username
  and the session email are *not* the author's public identity and must never
  appear in a commit, the README, or any published text.
- Smart App Control transiently blocked a freshly downloaded scipy DLL once, then
  allowed it after reputation check. If an import fails with "Application Control
  policy has blocked this file", retry before debugging.
