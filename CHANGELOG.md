# Changelog

Dated entries rather than version tags: the index is published as a release
asset that gets replaced in place, so a date says more about what you are
running than a number would.

---

## 2026-08-23 — like this one, but denser

Until now you could say *like this one* and *not like that one*. You can now say
*like this one, **but denser*** — or with wider jumps, or fewer streams. Additive
again: the feature version is still `8e824c8319c6`, no index was rebuilt, and a
client that ignores `steer` sees exactly what it saw yesterday.

### What it does

`steer` names an axis and a signed amount. Only maps that really clear that much
of a gap stay in the pool; they are then ranked by ordinary distance to the
target, exactly as before.

```bash
curl -s "https://api.ayreth.site/api/v1/similar?beatmap_id=21454&steer=object_density:+0.3"
```

or, in a JSON body, `"steer": {"object_density": 0.3, "sp_p50": -0.5}`.

The axis names come from the same table as the sentences printed under every
result — a control labelled "denser" and an explanation reading "denser
(3.7 → 5.3 obj/s)" name the same column, and there is only one list of them.
`/api/v1/meta` now serves it as `steer_axes`, and announces `steer` in
`supports`.

### Why a filter and not a nudge

Three designs were built and measured on the full 160,905-map index, on three
targets from 3.97★ to 6.91★, six axes, both directions.

**Translating the query** — move the probe along the axis and rank by distance
to that point — is *inert*, which was the surprise. Up to a displacement of 2
robust units the top ten are **exactly** the results of the plain search, and
their median density moves by +0.44 obj/s. The arithmetic says why: ranking by
`‖x − (v + δe)‖²` is ranking by `‖x − v‖² − 2δ(xᵢ − vᵢ)`, and since the 96
features are correlated, a map that is clearly denser is also further away on
the other 95. Nothing reorders until the probe has left the data entirely. On
`drain_time_s` it was worse than useless: at δ = 0.5 it returned maps **125 s
shorter** than the query.

**A sign filter** (`x > target`) never leaves the manifold but has no dial, as
expected: it returned +0.20 obj/s, or +5%. Nobody calls that denser.

**A margin filter** (`x ≥ target + δ`) is what shipped. It has a dial, and it
doses for a mechanical reason: ranking by distance to the target prefers the
smallest possible overshoot, so the gap you get lands *on* the margin you asked
for instead of sailing past it.

The translation was not wasted, though — it is the right way to *retrieve*. The
probe sent to the ANN index is projected onto the constraint, which is the point
of the constraint closest to the target, so beyond 30,000 candidates the
approximate pass looks where the survivors actually live. Verified: exact and
ANN paths return the same ten maps.

### Measured

On a 5.57★ query, 10,901 candidates, best plain neighbour at distance 2.005:

| `steer` on `object_density` | candidates left | gap obtained | distance to target |
|---|---|---|---|
| +0.1 | 47.2% | +0.59 obj/s | 2.294 |
| +0.2 | 20.5% | +1.09 obj/s | 2.615 |
| **+0.3** | **7.9%** | **+1.54 obj/s** | **2.850** |
| +0.5 | 1.8% | +2.54 obj/s | 4.174 |
| +1.0 | 0.3% | +5.10 obj/s | 7.340 |

That last row is why 1.0 is the cap: its results carry clipped coordinates,
which is the corpus's outliers rather than maps anyone plays. The useful range
is 0.1 to 0.5.

A side effect worth having: steering makes a search *faster*, because it removes
candidates rather than sorting them — 2.2 ms against 10.4 ms on the real index.

### On both interfaces

`/explore` **and the tosu overlay** gain a **Steer** panel with six sliders:
density, streams, jump size, cursor speed, aim-vs-tapping and length. Each one
reads back the phrase you asked for — "denser", "much wider jumps", "slightly
shorter" — in the server's own words, read from `/meta` rather than copied into
the page. The active direction is repeated above the results, because a list
filtered by something folded away in a panel is a trap this project has already
paid for.

The overlay was going to be left out — no input field, a corner of a second
screen — and then it turned out six sliders fit fine, because unlike a list of
seed maps they need no typing. It is **counter version 1.3.0**; tosu offers the
update on its own. Its local route `/api/similar` learned `steer` too, so the
sliders work whether the overlay is served by `start.bat` or by tosu itself.

A panel neither page can use is a panel neither page shows: both read
`supports` from `/meta` and hide the whole thing when the server they reached is
older than they are. Verified against the live API before this release was
deployed — the overlay simply had no Steer panel, rather than a 400.

That check had a hole, found by running it for real rather than by reasoning
about it: `/meta` carried no `Cache-Control`, so after the deploy the overlay
kept hiding the panel because the browser was still reading a `supports` from
ten minutes earlier. `/meta` now answers `no-store` and both pages probe it with
`cache: "no-store"`. A capability probe served from cache is a capability probe
answering about the previous version of the server.

Nine more axes exist and stay available through the API. Fifteen sliders is an
interface nobody opens.

### Two things that were confusing

**Weights and Steer read as the same idea and are not.** Both panels now say
which is which, in one line each: weights decide *what counts as similar*
(the same neighbours, measured differently); steering decides *what should be
different* (other maps entirely). They are also cross-referenced, since the
confusion only shows up when you see one and think of the other.

**"Diverge on" explained nothing.** It had a tooltip; nothing said so. The label
is now dotted-underlined with a help cursor, the text says what actually happens
("same difficulty, opposite challenge — close on every group except this one,
then ranked by how far they are on it"), and the dropdown itself carries it too,
for whoever hovers the control rather than its name.

### Known limitations

- **Push too far and the list comes back empty**, and that is the honest answer
  rather than a bug: nothing in the corpus is that much denser *and* still
  similar. The page says which sliders to ease. There is no "give me the closest
  I can get" fallback — it would mean labelling maps as denser when they are
  not.
- **The same amount does not bite equally on every axis.** `+0.3` on jump size
  leaves 43% of the candidates standing; on density it leaves 8%; on a target
  that already sits at the edge of the scale it can leave none. The number is a
  guaranteed *margin*, not a difficulty setting, and how many maps clear it is a
  property of the corpus.
- **The sliders act one axis at a time and they compound.** Asking for three
  strong directions at once is asking for a map that may simply not exist; the
  page will tell you, but it cannot trade one axis off against another.
- The direction is **not** carried in the shareable URL. `?b=131891` still only
  identifies the question, not the settings, so a link you paste in a chat opens
  on an unsteered search.
- **The overlay still does not get the multi-map mode or the counter-example**,
  and for the reason steering turned out not to share: those need a list to
  assemble and a result to point at. Steering needs neither.
- The two interfaces now duplicate the axis list, one copy each. They have
  always duplicated their stylesheet for the same reason — the overlay must stay
  a single file tosu can serve — so a test compares the two lists and fails if
  an axis is added on one side only.

---

## 2026-08-22 (evening) — ask with several maps, or against one

Three questions the engine could not express before, and a page to ask them
from without the game running. All of it is additive: the feature version is
still `8e824c8319c6`, no index was rebuilt, and a client that ignores the new
options sees exactly what it saw yesterday.

### Several maps at once

`POST /api/v1/similar/multi` takes a list of checksums and beatmap ids and
searches from their centroid — *here are five maps I love, find me more*.

The trap is the star band. With seeds at 4★ and 7★ the centroid sits at 5.5★,
and the usual `centre ± star_range` band excludes **both ends** — precisely the
maps closest to what was asked for. The band now covers
`[min − range, max + range]`. Measured on the real corpus: 22,234 candidates for
the centred band against 82,662 for the span, and the best match improved from
distance 1.218 to 1.156. Length is handled the same way.

Seeds are not weighted. A map listed twice would weight itself by accident, so
duplicates are dropped and named in `warnings`. Every seed's beatmapset is
excluded, not just one — `exclude_same_set` only ever knew about a single map.

A seed that is not in the corpus is skipped and reported; if none of them
resolves, the answer is a 404. Losing one seed out of five still leaves a useful
answer, losing all of them does not.

### Filtering by what actually differs

`group_filter=aim` keeps only the results whose gap comes mostly from that
group. It costs nothing — `group_share` was already computed for every match.

It had to be applied to the **ranking** rather than to the finished list, or it
would shrink the list instead of filling it: the same trap as the per-song
de-duplication, which already takes `k × 8` before cutting. On a 5.5★ query the
unfiltered top ten breaks down as six `parameters`, two `rhythm`, one `aim` and
one `speed` — and yet every one of the five groups still returns a full ten.

### A counter-example

`avoid_checksum` / `avoid_beatmap_id` is `diverge` made concrete: instead of
naming an abstract group, you point at a map. *Like this one, but not like that
one.* Results are ranked by `d(x, target)² − 0.3 · d(x, avoided)²`.

A hard margin — keep only maps closer to the target than to the avoided one —
was implemented, measured and thrown away. Its severity depends entirely on how
far apart the two maps happen to be, which nobody controls: on two real queries
it kept 33% of the candidates in one case and 6% in the other.

The penalty is predictable, and provably so. At a fixed strength it is *exactly*
a search around the extrapolated point `(target − β·avoided) / (1 − β)` —
verified numerically on the first fifty results. That equivalence is also why
the strength is capped at 0.75: as β approaches 1 that point runs off the
manifold and the search stops meaning "like this one".

### Measured

On a 5.5★ query whose counter-example is its own top neighbour:

| strength | distance to target | distance to avoided | shared with the plain list |
|---|---|---|---|
| 0.0 | 1.114 | 1.121 | 10/10 |
| 0.15 | 1.122 | 1.235 | 7/10 |
| **0.3** (default) | **1.146** | **1.396** | **6/10** |
| 0.5 | 1.188 | 1.471 | 5/10 |
| 0.9 (refused) | 2.585 | 2.966 | 0/10 |

0.15 did not bite at all on some targets. 0.3 costs 3% of closeness to the
target and buys 25% of distance from the map you did not want.

The avoided map is now excluded from its own results. That is not cosmetic: it
sits at distance zero from itself, so it is the one the penalty spares *most* —
at strength 0.15 it came back fourth.

### A page to use it from, outside the game

`/explore`, served both by the local server and by the hosted API. Until now the
interface only worked with tosu and osu! running, because it read the map
selected in game and could do nothing else. This one starts from a link.

Two modes, and the switch between them is the first thing on the page:

- **From one map** — paste `osu.ppy.sh/beatmapsets/41823#osu/131891`,
  `osu.ppy.sh/b/131891`, `osu.ppy.sh/beatmaps/131891`, `osu://b/131891`, a bare
  id, or an MD5. A link to a *mapset* is refused with an explanation rather than
  guessed at, because the difficulty is the thing being searched.
- **From several maps** — add as many as you like. Each one is resolved as you
  add it, so you see its title, length and star rating immediately, and a map
  that is not in the corpus says so straight away instead of at search time.

Any result can become the next query (*Find maps like this one*), the
counter-example (*Less like this*) or another seed (*Add to my list*). Searches
are shareable: the URL carries `?b=131891` or `?ids=100695,1005019,1178810`, and
opening one restores the search.

**It works with no network at all.** Served by `start.bat`, the page queries the
local index — verified: loading it and running a full search issues three
requests, all to `127.0.0.1`, and none to `api.ayreth.site`. Only the beatmap
thumbnails come from the internet, and they were already written to disappear
quietly when they do not load. The page picks its own server the way the overlay
does: its own origin first, then the local server, then the hosted API.

It reads `supports` from `/api/v1/meta` and hides what the server it is talking
to cannot do, rather than offering a button that answers 404.

### Known limitations

- **The tosu overlay does not get any of this, deliberately.** It is driven by
  the map selected in game, has no input field, and a corner of a second screen
  is the wrong place to assemble a list. It is untouched by this release — same
  file, same version, nothing to re-install.
- The two pages share their look by **copy**, not by a shared stylesheet: the
  overlay has to stay a single self-contained file that tosu can serve, and the
  hosted API has no static directory to mount. Changing a colour means changing
  it twice.
- A centroid is a compromise and can be one nobody asked for: seeds at 3★ and
  8★ return results around 6★. The response says so in `warnings` when the seeds
  span more than 2★, but it cannot do better than warn.
- Seeds and counter-examples must be maps **in the corpus**. There is no
  file-upload form of `/similar/multi`; featurising N files to average them
  would cost N times a search, and the real use case is maps you already play.

### Deployed

Live on `api.ayreth.site`, verified from outside the machine: the same three
seeds return the same five maps there as on a local index, and
<https://api.ayreth.site/explore> answers with both modes working.

The index was **not** rebuilt and the release asset was **not** replaced —
checked rather than assumed: `feature_version` is still `8e824c8319c6` and the
map counts are unchanged (`nomod` 160,905, `DT` 160,048, `HR` 160,905,
`DTHR` 160,048, `HT` 161,064). Nothing here touches the features, so an index
built before today still serves these endpoints correctly.

---

## 2026-08-22 (later) — ranked, loved and unranked are told apart

The dump carries ranked and loved maps without ever recording which is which,
so the index called both `dump` — a placeholder that refused to claim a status
it could not know. `scripts/fetch_loved.py` closes that gap.

It fetches only the **checksums** of loved difficulties and downloads nothing:
those maps are already in the dump, so there is nothing to retrieve but the
labels. Fourteen requests, five seconds.

```bash
.venv\Scripts\python.exe scripts\fetch_loved.py
.venv\Scripts\python.exe scripts\build_all.py
```

The `nomod` index then reads: **147,573 ranked, 3,985 loved, 9,192 graveyard,
155 pending.**

Each recommendation now carries an icon before its title. They are inline SVG
rather than emoji, because an emoji renders from whatever font the machine
happens to have and a tombstone can arrive as an empty box. Ranked is the
quietest of the four on purpose — the normal case should not compete for
attention. Where the loved list is absent, the status stays `dump` and no icon
is shown at all, which is the honest rendering of "not known".

### Known limitation

Coverage is 96.8%: 3,985 of the 4,118 loved difficulties are found in the dump.
The other 133 were loved after the dump was taken, or re-uploaded since, so
their MD5 no longer matches anything in it. They read as ranked — 0.09% of the
ranked pool, and it corrects itself with the next dump.

---

## 2026-08-22 — the most-played unranked maps join the pool

The ppy dump holds only ranked and loved beatmaps. Maps like *Jump Training #1*
(34M plays) or *Harder, Better, Faster, Stronger* (18M) could be used as a
query but could never be recommended, purely because nobody pushed them through
ranking.

The pool now also holds the **2,000 most-played unranked beatmapsets** —
graveyard and pending, ranked by playcount. That is **9,347 difficulties** in
the `nomod` index, which goes from 151,558 to **160,905 maps**.

They are not evenly spread. Above 6★ they are 14.6% of the index, and above 9★
they are 43.6% — ranked maps thin out at high star ratings and unranked ones do
not.

### For anyone using it

- Unranked recommendations carry a **badge with their playcount**, because they
  do not download the way a ranked map does.
- **Filters → Include unranked maps** turns them off. On by default.
- The API takes `allow_unranked=false` for the same thing.
- Nothing else changed. The feature version is still `8e824c8319c6`, so no
  client had to be updated, and an index built before this release keeps
  working — it just reports every map as coming from the dump.

### For anyone building their own index

```bash
.venv\Scripts\python.exe scripts\fetch_unranked.py --sets 2000
.venv\Scripts\python.exe scripts\build_all.py
```

About 10,200 files and ~460 MB, three quarters of an hour. Interruptible and
resumable — a file already on disk whose MD5 still matches is not fetched
again, so raising `--sets` later only costs the difference.

Files come from three mirrors in parallel, one thread each. That is as much
about courtesy as speed: at 3 requests per second per mirror, each one sees
*less* traffic than a single-source run would send it, while the combined rate
triples. Anything that fails is retried on a different mirror.

Every download is checked against the MD5 the search announced and dropped if
it does not match. This matters more than it looks: the whole index is keyed on
the checksum, which *is* the MD5 of the `.osu`, so a mirror that repackaged
files would produce entries the game could never recognise. Verified before
building on it — osu.ppy.sh, catboy.best and osu.direct return byte-identical
files for the same map.

### Measured

- Distinct songs in the unranked pool: 598 → **2,082**
- A top-10 surfaces an unranked map on **15.6%** of queries, against 6% at the
  smaller pool size
- Scaler drift from the larger corpus: **0.02 robust units**, with 97.5% top-10
  overlap against the previous index. Existing recommendations do not move.

### Fixed along the way

Three bugs that each failed silently, found by running the thing rather than by
reading it:

- The pool folder was named `unranked_osu_files`, and dump detection matches on
  `osu_files`. Counting is capped, the comparison is strict, and `data/` is
  scanned first, so the pool won ties — the whole main corpus got built on the
  wrong folder. Directories are now identified by the manifest they carry, not
  by their name.
- `build_corpus` recorded its source in its manifest but never read it back
  when resuming, so it reused stale chunks and reported their counters against
  the new batch. The only visible symptom was skip rates above 100%. A manifest
  now has to reconcile: `n_kept + skips == n_seen`. Invalidated chunks are
  deleted rather than merely ignored, because `load_corpus` globs the folder and
  would have swallowed the orphans.
- Status annotations were joined on `beatmap_id`, but graveyard `.osu` files
  exist whose `BeatmapID` field is not their own. Joining on the checksum
  instead also stops the dump being relabelled by a mirror's stale status.

### Known limitation

**Ranked and loved are not told apart.** The dump does not say which is which,
so both are reported as coming from the dump rather than claiming a status that
is not known. Only *unranked* is distinguished.

*Lifted the same day — see the entry above.*

---

## 2026-08-21 — first public release

151,558 ranked and loved beatmaps, five mod variants, the local server, the
hosted API and the tosu overlay.
