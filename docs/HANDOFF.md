# Handoff

State of the project, the things worth not rediscovering, and the two pieces of
work queued next. Written to be enough for a cold start — a new contributor or a
fresh assistant session should be able to work from this file alone.

Last updated: 2026-08-22, at commit `6776dbf`.

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
| Search | ~15–100 ms |
| Tests | 37, all passing |
| Distribution | prebuilt `nomod` index as a GitHub release asset, 164 MB |
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
| Run the app | `.venv\Scripts\python.exe -m osureco` (or double-click `start.bat`) |
| Tests | `.venv\Scripts\python.exe -m pytest tests -q` |
| Rebuild every variant | `.venv\Scripts\python.exe scripts\build_all.py` |
| Rebuild one variant | `scripts\build_all.py --variants DT` |
| Quality check on known maps | `scripts\sanity_check.py --why` |
| Feature sanity on a sample | `scripts\featurize_sample.py --n 3000` |
| Pack a release asset | `scripts\pack_release.py --variant nomod` |
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
src/osureco/
  features.py   the core: featurize(), FEATURE_SPEC, Mods, .osu parser      775 l
  index.py      Scaler, Searcher, SearcherSet, search, explain()            693 l
  server.py     FastAPI: /api/similar, /api/current, /events (SSE), /       280 l
  corpus.py     batch build, multiprocessing, chunked parquet, resume       280 l
  tosu.py       websocket client, debounce, live featurisation             249 l
  launcher.py   one-click bootstrap: fetch index, start server              200 l
  paths.py      every path resolution, no hardcoded locations               118 l
  static/index.html   the whole UI, vanilla, single file                    715 l
```

Data flow, live: `tosu websocket → debounce 300 ms → GET /files/beatmap/file →
featurize() → scaler → weighted distance over pre-filtered candidates → JSON → page`.

Data flow, batch: `dump → ProcessPoolExecutor → featurize() → parquet chunks →
RobustScaler → vectors.npy + vectors_raw.npy + usearch index + SQLite`.

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

## 7. Next: local mode or hosted API

The goal is that a non-technical player is not asked to download 164 MB or to
have Python work. Local stays the default and the fallback — if the hosted API
goes down, the tool must keep working, which is precisely how the original
OsuMapMatcher died.

### The real design question

The live map's `.osu` is on the player's machine, served by tosu. Something has
to turn it into a vector. Where that happens decides everything:

**Option A — featurise locally, query remotely.** The client keeps Python,
`rosu-pp-py` and `numpy` (~40 MB of wheels), runs `featurize()`, and POSTs 96
floats. No index download, tiny payloads, nothing about the player's files leaves
the machine. Server needs a `POST /api/similar` accepting a raw vector.

**Option B — upload the `.osu`, featurise server-side.** The client sends a ~40 KB
file and needs *no* scientific dependencies at all. This is the option that could
eventually make the tool a pure HTML/JS **tosu counter**, which is the only way it
would ever fit the tosu counter library — those are static pages served by tosu
itself and cannot host a Python backend. Costs: beatmap files transit to the
server, and the server pays the CPU.

Option B is more work and more server load, but it is the one that unlocks the
distribution channel the project actually wants. Worth deciding deliberately
rather than defaulting to A because it is easier.

### Either way

- `search()` already takes a `QueryTarget`, so a remote endpoint is mostly
  request plumbing rather than new logic.
- The client needs a **mode switch** (`local` / `remote`) surfaced in the UI, with
  automatic fallback to local when the remote is unreachable, and an honest
  status indicator — the header already has one.
- The server side needs CORS, rate limiting, and a pinned corpus version so a
  client's vector and the server's index always share the same `FEATURE_NAMES`.
  **Send a feature-set version with every request and reject mismatches** — a
  silently misaligned vector produces plausible-looking nonsense.
- `OSURECO_INDEX_URL` is already the precedent for pointing the client elsewhere;
  a `OSURECO_API_URL` would sit naturally beside it.

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
