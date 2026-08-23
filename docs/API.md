# osu!MapMatcher HTTP API

The same recommendation engine the desktop tool runs locally, exposed over
HTTP so a client does not need Python, `rosu-pp`, or a 164 MB index download.

**The API is an option, never a requirement.** The tool works with no server at
all, and a client that can run locally should keep doing so — that is the
failure mode the original OsuMapMatcher died of. Treat a hosted instance as a
convenience for people who cannot install anything, and always keep a local
fallback path.

To deploy your own instance, see [`deploy/README.md`](../deploy/README.md).

---

## Contents

- [The three ways to ask](#the-three-ways-to-ask)
- [Compatibility contract](#compatibility-contract)
- [Endpoints](#endpoints)
- [Search options](#search-options)
- [Response shape](#response-shape)
- [Errors](#errors)
- [Client recipes](#client-recipes)

---

## The three ways to ask

Something has to turn the `.osu` file on the player's disk into 96 numbers.
Where that happens is the only real design choice, and the API supports all
three answers so a client can pick by what it is able to install.

| | What the client sends | Server CPU | Client needs |
|---|---|---|---|
| **1. Checksum** | 32 hex chars | none | nothing |
| **2. Features** | a dict of 96 named values | none | Python + `rosu-pp-py` + `numpy` |
| **3. File** | the raw `.osu`, ~40 KB | ~20 ms | nothing |

A fourth form starts from *several* maps rather than one:
[`POST /similar/multi`](#post-apiv1similarmulti) takes a list of checksums and
beatmap ids and searches from their centroid. It is path 1 repeated, so it also
costs the server nothing.

**Path 1 covers the overwhelming majority of real traffic.** Every ranked and
loved map is already in the corpus, its vector is already computed, and its
identity is its MD5 — which tosu hands you for free. Only unsubmitted, edited
or very new maps miss.

**Path 3 subsumes path 1 automatically.** `POST /similar/file` hashes the bytes
first and looks that hash up in the corpus; if the map is known, nothing is
featurised. So a client with no dependencies at all can just always post the
file and still cost the server nothing on known maps. This is the path that
makes a pure HTML/JS **tosu counter** possible — those are static pages served
by tosu itself and cannot host a Python backend.

**Path 2 is for a client that already has the Python stack** — the desktop tool
itself, when it wants to use a remote index instead of downloading one. It
sends the dict `featurize()` returned, keyed by name, so no column can ever
shift silently.

---

## Compatibility contract

A feature vector computed against one feature set and searched against an index
built from another produces results that look completely plausible and are
wrong. Nothing crashes, nothing logs, the numbers are simply meaningless.

So every response carries `feature_version`, a short hash of the ordered list of
the 96 feature names:

```json
{ "feature_version": "8e824c8319c6" }
```

- Read it once at startup from `GET /api/v1/meta`.
- Send it back on any request that carries features or a vector.
- A mismatch is answered with **409**, never with results.
- With a raw `vector` (positional), `feature_version` is **required** — there is
  no other way to know the columns line up.
- With a `features` dict (keyed by name), it is optional but recommended: a
  missing key already fails loudly with **400**, but a *renamed* one would not.

The version changes whenever a feature is added, removed or reordered — which
is also the moment every index on disk becomes invalid, so client and server
have to move together.

---

## Endpoints

Base path: `/api/v1`. Everything returns JSON. Authentication is optional and
off by default; when a server sets a token, send
`Authorization: Bearer <token>` (or `X-API-Key: <token>`) on every call except
`/health` and `/api/v1/meta` — those stay open so a client can check
compatibility before it knows whether it is allowed in.

### `GET /health`

Liveness. Answers even with no index installed, which is the point: an instance
without an index should be visible and drained from a load balancer, not
restarted in a loop.

```json
{
  "ok": true,
  "ready": true,
  "variants": ["nomod", "DT", "HR", "DTHR", "HT"],
  "feature_version": "8e824c8319c6",
  "uptime_s": 1820.4
}
```

`ready: false` means no index is loaded: search endpoints answer **503**.

### `GET /api/v1/meta`

What a client reads at startup.

```bash
curl -s https://api.example.com/api/v1/meta
```

```json
{
  "feature_version": "8e824c8319c6",
  "n_features": 96,
  "feature_names": ["cs", "hp", "od", "ar", "..."],
  "groups": ["aim", "speed", "rhythm", "spacing", "parameters"],
  "variants": ["nomod", "DT"],
  "n_maps": { "nomod": 151558, "DT": 151329 },
  "supports": ["multi", "group_filter", "avoid", "steer"],
  "steer_axes": [
    { "axis": "object_density", "more": "denser", "less": "less dense" },
    { "axis": "sp_p50", "more": "wider jumps", "less": "tighter jumps" }
  ],
  "limits": {
    "k_max": 100,
    "max_seeds": 20,
    "avoid_strength_max": 0.75,
    "steer_max": 1.0,
    "max_upload_bytes": 4000000,
    "rate_per_minute": 120,
    "file_cost": 5,
    "auth_required": false
  }
}
```

`feature_names` is the column order for the compact `vector` form.

`steer_axes` is the vocabulary of the directional search: every axis you may
push, with the word for each direction. Those are the same words that appear in
a match's `differences`, on purpose — a control labelled "denser" and a sentence
saying "denser" name the same column. Read the list rather than hard-coding it;
it is 15 entries today and may grow.

`supports` lists the optional capabilities this instance has. A hosted instance
is updated by pulling and rebuilding, so a client can easily be newer than the
server it is talking to; read this array and hide what the server does not
offer, rather than discovering it on a 404. An instance that predates the field
omits it entirely — treat a missing `supports` as "none of them".

### `GET /api/v1/map/{checksum}`

Is this map in the corpus? Lets a client decide whether it has to upload
anything. **404** if unknown.

```json
{
  "variant": "nomod",
  "warnings": [],
  "checksum": "9af49bce8882fa219a24aa815a03e698",
  "beatmap_id": 1000459,
  "label": "Neptune - Hollywood [Fort's Style]",
  "stars": 5.91,
  "bpm": 140.0,
  "length_s": 114.9,
  "in_corpus": true,
  "mods": ""
}
```

### `GET /api/v1/similar`

Neighbours of a map that is already in the corpus. Costs the server no CPU
beyond the search itself.

| Parameter | Default | Meaning |
|---|---|---|
| `checksum` | — | MD5 of the `.osu`, as reported by tosu |
| `beatmap_id` | — | alternative to `checksum` |
| `mods` | `""` | active mods, e.g. `HDDT`; picks the corpus variant |
| `rate` | `1.0` | lazer clock rate |
| `variant` | — | force a corpus variant, bypassing `mods` |

Plus every [search option](#search-options).

```bash
curl -s "https://api.example.com/api/v1/similar\
?checksum=9af49bce8882fa219a24aa815a03e698&k=10&mods=DT"
```

**404** if the checksum is not in the corpus — post the file instead.

### `POST /api/v1/similar`

Same thing, with the target supplied by the client. Body is JSON; search
options live at the top level.

```jsonc
{
  // pick exactly one way of naming the target
  "checksum": "9af49…",              // a corpus map
  "beatmap_id": 1000459,             // a corpus map
  "features": { "cs": 4.0, "…": 0 }, // the dict featurize() returned
  "vector": [4.0, 5.0, "…"],         // the same, positional

  "meta": { "stars": 5.91 },         // extra fields merged into `features`
  "feature_version": "8e824c8319c6",
  "featurized_with": "DT",           // mods the client used, for a sanity check
  "mods": "HDDT",
  "rate": 1.0,
  "variant": null,

  "k": 20,
  "star_range": 0.5,
  "weights": { "aim": 1.0, "speed": 1.5 }
}
```

`stars` is **required** on the features and vector paths. It is not a feature —
it is the difficulty filter, and without it the search has nothing to narrow
down (see the `META_NAMES` note in `features.py`). Send it inside `features` or
inside `meta`.

`featurized_with` is optional and purely diagnostic: if the mods the client
featurised with are not the ones the corpus was built with, the response says so
in `warnings` instead of quietly returning biased distances.

### `POST /api/v1/similar/multi`

Neighbours of **several** maps at once: "here are five maps I love, find me
more". The seeds are averaged into a centroid and the search runs from there.

```bash
curl -s -X POST https://api.example.com/api/v1/similar/multi \
  -H "Content-Type: application/json" \
  -d '{"beatmap_ids": [1000459, 2552165], "k": 10}'
```

```jsonc
{
  "checksums": ["9af49…", "58e10…"],   // corpus maps, by MD5
  "beatmap_ids": [1000459, 5731234],   // or by id; mix both freely
  "mods": "", "rate": 1.0, "variant": null,
  "k": 20, "star_range": 0.5           // plus every search option
}
```

Only corpus maps are accepted — there is no file-upload form of this endpoint.

**The star band covers the seeds, not their centre.** With seeds at 4★ and 7★
the centroid sits at 5.5★, and a `centre ± star_range` band would exclude both
ends — precisely the maps closest to what was asked for. The band is therefore
`[min − star_range, max + star_range]`. Measured on the real corpus: 22 234
candidates for the centred band against 82 662 for the span, and the best match
improved from distance 1.218 to 1.156. Length works the same way.

**Seeds are not weighted.** Listing a map twice would weight it by accident, so
duplicates are dropped and reported in `warnings`.

Failure is graded, because losing one seed out of five still leaves a useful
answer and losing all of them does not:

- a seed that is not in the corpus is **skipped** and named in `warnings`;
- if *no* seed resolves, the response is **404**;
- more than `limits.max_seeds` seeds is **400**.

The reply has the same shape as every other search endpoint. Only `query`
changes meaning: it describes the set rather than a map, with
`is_centroid: true`, the seed list in `query.seeds`, and no `checksum`.

> **A centroid is a compromise, and it can be one nobody asked for.** Seeds at
> 3★ and 8★ produce results around 6★ — the average of two things you like is
> not necessarily something you like. When the seeds span more than 2★ the
> response says so in `warnings`; show it.

### `POST /api/v1/similar/file`

The zero-dependency path. Body is the raw `.osu` bytes — **not** a multipart
form (a multipart request is answered with **415** and a hint).

```bash
curl -s -X POST \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"Neptune - Hollywood.osu" \
  "https://api.example.com/api/v1/similar/file?k=10&mods=DT"
```

Query parameters: `mods`, `rate`, `variant`, and every
[search option](#search-options).

The server hashes the bytes and looks the hash up in the corpus before doing
any work, so a known map costs nothing to featurise. `source` in the response
tells you which happened: `"corpus"` or `"file"`.

The file is used and discarded. Nothing is written to disk; the only thing kept
is a small in-memory cache of recent featurisations, keyed by content hash.

### `POST /api/v1/featurize`

Featurise without searching. Body is the raw `.osu`, same as above. Useful for a
client that wants to compute once and then reuse the dict across many searches
with different weights.

```json
{
  "feature_version": "8e824c8319c6",
  "variant": "DT",
  "featurized_with": "DT",
  "features": { "cs": 4.0, "hp": 5.0, "…": 0 },
  "warnings": []
}
```

Feed `features` straight back into `POST /api/v1/similar`.

### `GET /api/v1/stats`

Counters since startup: requests, searches, featurisations, cache hits,
throttled and unauthorised requests, cache size, CPU concurrency. Handy for
checking that the corpus-hash shortcut is doing its job — `featurized` should
stay far below `searches`.

---

## Search options

Identical to the desktop UI's controls, with identical bounds. Available as
query parameters on the GET and file endpoints, and as top-level JSON fields on
`POST /api/v1/similar`.

| Option | Type | Default | Range | Meaning |
|---|---|---|---|---|
| `k` | int | 20 | 1–100 | how many results |
| `star_range` | float | 0.5 | 0–10 | half-width of the star band |
| `length_min` | float | 0.5 | 0.05–1 | shortest result, as a ratio of the query |
| `length_max` | float | 2.0 | 1–20 | longest result, same |
| `exclude_same_set` | bool | true | | drop other difficulties of the same mapset |
| `max_per_song` | int | 1 | 0–10 | cap per song title; 0 disables |
| `allow_unranked` | bool | true | | keep heavily-played graveyard/pending maps in the pool |
| `diverge` | string | — | one group | stay close on everything *except* this group |
| `group_filter` | string | — | one group | keep only results whose gap comes mostly from this group |
| `avoid_checksum` | string | — | | counter-example: a corpus map to move away from |
| `avoid_beatmap_id` | int | — | | the same, named by id |
| `avoid_strength` | float | 0.3 | 0–0.75 | how hard to move away from it |
| `steer` | object / string | — | ±1.0 per axis | "like this one, but denser" |
| `weights` | object | all 1.0 | 0–2 each | per-group weighting |

Weights are named on the JSON path (`"weights": {"aim": 1.5}`) and prefixed on
the query-string path (`w_aim=1.5`). Groups: `aim`, `speed`, `rhythm`,
`spacing`, `parameters`.

`diverge` is the "same difficulty, opposite challenge" mode: results stay near
the query on every group but the named one, and are then ranked by how *far*
they are on it.

`group_filter` sorts the answer rather than changing it: it keeps only the
results whose largest `group_share` is the named group — "show me the ones that
differ mostly in aim". The filter is applied to the ranking, before the
per-song de-duplication, so the list fills up instead of shrinking.

### The counter-example

`avoid_*` is `diverge` made concrete: instead of naming an abstract group, you
point at a map. Results are ranked by

```
d(x, target)² − avoid_strength · d(x, avoided)²
```

Two designs were measured. A hard margin — keep only maps closer to the target
than to the avoided one — was rejected: its severity depends entirely on how far
apart the two maps happen to be, which the caller does not control. On two real
queries it kept 33 % of candidates in one case and 6 % in the other.

The penalty is predictable, and provably so: at a fixed strength it is *exactly*
a search around the extrapolated point `(target − β·avoided) / (1 − β)`. That is
also why the strength is capped — as β approaches 1 that point runs off the
manifold. What it costs and what it buys, on a 5.5★ query whose counter-example
is its own top neighbour:

| `avoid_strength` | mean distance to target | mean distance to avoided | shared with the plain list |
|---|---|---|---|
| 0.0 | 1.114 | 1.121 | 10/10 |
| 0.15 | 1.122 | 1.235 | 7/10 |
| **0.3** (default) | **1.146** | **1.396** | **6/10** |
| 0.5 | 1.188 | 1.471 | 5/10 |
| 0.9 (refused) | 2.585 | 2.966 | 0/10 |

The avoided map is excluded from its own results. This is not cosmetic: it sits
at distance zero from itself, so it is the one the penalty spares *most* — at
strength 0.15 it came back fourth.

`distance` and `similarity` in the response are always measured against the
**target**. The penalised score ranks; it is never displayed, and it can be
negative.

A counter-example that is not in the corpus is a **404**, unlike a missing seed
in `/similar/multi`. There is only one of them, so ignoring it would silently
drop the only constraint the caller bothered to express.

### The direction

`steer` is "like this one, **but denser**". You name an axis and a signed
amount, and only maps that actually clear that much of a gap stay in the pool;
they are then ranked by ordinary distance to the target.

Two ways to write it, same meaning:

```bash
# query string — one parameter, any number of axes
curl -s "https://api.example.com/api/v1/similar?checksum=9af49…&steer=object_density:+0.3,sp_p50:-0.5"
```

```jsonc
// JSON body
{ "checksum": "9af49…", "steer": { "object_density": 0.3, "sp_p50": -0.5 } }
```

Axis names come from `steer_axes` in `/meta`. The sign follows that entry:
positive is `more`, negative is `less`. An unknown axis is a **400**.

The amount is a **margin in robust units** — the units the vectors themselves
live in, so the same number means a comparable move on every axis. `+0.3` on
`object_density` is "at least 0.3 units denser", about +1.4 obj/s on the real
index. Useful values sit between 0.1 and 0.5; the cap is 1.0.

Three designs were measured on the full 160 905-map index, three targets from
3.97★ to 6.91★, six axes, both directions.

- **Translating the query** — move the probe by `δ` on the axis and rank by
  distance to that point — is *inert*. Up to δ = 2 the top ten are exactly the
  results of the plain search: gaining on one axis costs more on the 95
  correlated ones, so nothing reorders until the probe is off the manifold.
- **A sign filter** (`x > target`) never leaves the manifold but has no dial:
  it returned +5 % density, which nobody would call denser.
- **A margin filter** (`x ≥ target + δ`), kept. It has a dial, and ranking by
  distance to the target prefers the smallest overshoot, so the gap you get
  lands *on* the margin you asked for instead of overshooting it.

On a 5.57★ query, 10 901 candidates, best plain neighbour at distance 2.005:

| `steer` on `object_density` | candidates left | gap obtained | distance to target |
|---|---|---|---|
| +0.1 | 47.2 % | +0.59 obj/s | 2.294 |
| +0.2 | 20.5 % | +1.09 obj/s | 2.615 |
| +0.3 | 7.9 % | +1.54 obj/s | 2.850 |
| +0.5 | 1.8 % | +2.54 obj/s | 4.174 |
| +1.0 | 0.3 % | +5.10 obj/s | 7.340 |

That last row is why 1.0 is the cap: its results carry clipped coordinates,
which is the corpus's outliers rather than maps anyone plays.

Pushing hard on a narrow axis can leave nobody standing — the target may already
sit at the edge of the scale, or the corpus may simply have nothing that far
out. The answer is then **200 with an empty `matches`**, like `group_filter`.
Returning the least-bad maps and calling them denser would be a lie.

As with the counter-example, `distance` and `similarity` are always measured
against the target. Steering removes candidates; it never bends the distance.

---

## Response shape

Every search endpoint answers with the same object.

```jsonc
{
  "query": {
    "checksum": "9af49…", "beatmap_id": 1000459,
    "label": "Neptune - Hollywood [Fort's Style]",
    "stars": 5.91, "bpm": 140.0, "length_s": 114.9,
    "in_corpus": true, "mods": "DT",

    // the same fields whether you asked with one map or ten
    "is_centroid": false,   // true => `differences` compare to the AVERAGE
    "n_seeds": 0,           // 0 on the single-map endpoints
    "star_span": [5.91, 5.91],
    "seeds": []             // [{checksum, beatmap_id, label, stars, …}, …]
  },
  "variant": "DT",          // corpus actually searched
  "diverge": null,
  "group_filter": null,
  "avoid": null,            // or {checksum, beatmap_id, label, strength}
  "steer": null,            // or {"object_density": 0.3}, as applied
  "count": 10,
  "matches": [ /* … */ ],
  "source": "corpus",       // corpus | features | vector | file
  "feature_version": "8e824c8319c6",
  "corpus": { "variant": "DT", "n_maps": 151329 },
  "warnings": []            // approximations the server had to make
}
```

Each match:

```jsonc
{
  "rank": 1,
  "checksum": "58e10…", "beatmap_id": 5731234, "beatmapset_id": 2552165,
  "artist": "YUC'e", "title": "Summer Night Hiking",
  "creator": "Omekyu", "version": "Kujinn's Expert",
  "stars": 5.55, "bpm": 170.0, "length_s": 199.3,
  "distance": 2.0916,       // weighted euclidean distance, unbounded
  "similarity": 0.7057,     // exp(-distance / 6), monotone, for display
  "group_distance": { "aim": 1.19, "speed": 0.61, "…": 0 },
  "group_share":    { "aim": 0.33, "speed": 0.09, "…": 0 },
  "differences": ["steadier aim (x4.5 -> x3.5)", "longer (1:54 -> 3:22)"],
  "similarities": ["same AR", "same BPM"],
  "status": "dump",         // dump | graveyard | pending | wip
  "is_unranked": false,     // true => not downloadable the usual way
  "playcount": null,        // known only for unranked maps
  "url_web": "https://osu.ppy.sh/b/5731234",
  "url_direct": "osu://b/5731234",
  "url_cover": "https://assets.ppy.sh/beatmaps/2552165/covers/list@2x.jpg"
}
```

`similarity` is only a readable rescaling of `distance`; it is strictly
decreasing, so it never changes an ordering.

**`query.is_centroid` changes what `differences` means.** On a single map the
sentences compare two real maps. On a centroid they compare a real map to the
*average of the seeds* — "faster (170 → 195 BPM)" then means 170 is the mean BPM
of what you asked for, and no map in your list necessarily has it. An interface
that renders both the same way tells the reader something false.

`avoid` echoes the counter-example as the server **understood** it, not as it
was asked for: a caller who named it by `beatmap_id` can check which map that
actually resolved to.

`steer` echoes the direction the same way — parsed, and with the axes left at
zero dropped. A page that restores its controls from a response therefore reads
back what actually happened, not what was typed.

`status` is `dump` for anything that came out of the official ppy dump. That
covers both ranked and loved maps — the dump does not separate them, so the
field does not pretend to. Any other value is a map pulled in by playcount from
`scripts/fetch_unranked.py`, and `is_unranked` is the boolean to branch on.
An index built before this field existed reports `dump` for everything and
`null` playcounts, rather than failing.

### `warnings`

Never ignore this array. It is how the server says it answered a slightly
different question from the one asked:

- `variant 'DTHR' is not served here, falling back to 'nomod'` — the instance
  does not host that corpus. Distances are still consistent, but the mods are
  not modelled.
- `custom clock rate 1.2x is not modelled; answered against the 'DT' corpus` —
  lazer free rates have no corpus. `DT` is 1.5× and nothing else.
- `features were computed with mods 'HDDT' but the corpus was built with 'DT'` —
  the client featurised with the wrong mods; distances are biased.
- `2 seed(s) not in this corpus, ignored: …` — the centroid was built from
  fewer maps than were asked for. The answer is still useful; it is just not
  the one requested.
- `… was listed twice, counted once` — a duplicate seed was dropped so it would
  not weight itself.
- `seeds span 3.02 to 7.98 stars; their average is a compromise that may
  resemble none of them` — the honest warning about wide centroids.

Show them, or at least log them.

---

## Errors

FastAPI's usual `{"detail": "..."}`.

| Code | Meaning | What to do |
|---|---|---|
| 400 | malformed target: vector of the wrong length, missing feature, missing `stars`, unknown diverge or group filter, unknown or over-range `steer` axis, no seed, too many seeds | fix the request; the message names the problem |
| 401 | token missing or wrong | send `Authorization: Bearer …` |
| 404 | checksum unknown in this corpus; no seed resolved; counter-example unknown | post the `.osu` to `/similar/file`, or drop the unknown map |
| 409 | `feature_version` mismatch | update the client, or point it at a matching server |
| 413 | file larger than the server's limit | check `limits.max_upload_bytes` |
| 415 | multipart upload | resend as a raw body |
| 422 | the `.osu` cannot be used | see the reason: `not_std`, `too_few_objects`, `too_short`, `bad_header` |
| 429 | rate limited | honour `Retry-After`; consider running locally |
| 503 | no index loaded | server problem, retry later, fall back to local |

---

## Client recipes

### A tosu counter, in the browser, with no dependencies

```js
const API = "https://api.example.com/api/v1";

// tosu serves the current .osu at this URL, on the player's own machine
const FILE = "http://127.0.0.1:24050/files/beatmap/file";

let lastKey = "";

const socket = new WebSocket("ws://127.0.0.1:24050/websocket/v2");
socket.onmessage = async (event) => {
  const data = JSON.parse(event.data);
  const checksum = data?.beatmap?.checksum;
  const mods = data?.play?.mods?.name ?? "";
  if (!checksum) return;

  // the mods are part of the identity: turning DT on must re-query
  const key = `${checksum}|${mods}`;
  if (key === lastKey) return;
  lastKey = key;

  // 1. cheap path: the map is almost certainly in the corpus
  let response = await fetch(
    `${API}/similar?checksum=${checksum}&mods=${mods}&k=10`);

  // 2. only unsubmitted or edited maps get here
  if (response.status === 404) {
    const osu = await (await fetch(FILE)).arrayBuffer();
    response = await fetch(`${API}/similar/file?mods=${mods}&k=10`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: osu,
    });
  }

  if (!response.ok) return;                 // 429, 503: keep the last list
  const payload = await response.json();
  payload.warnings.forEach((w) => console.warn("mapmatcher:", w));
  render(payload.matches);
};
```

Debounce this. Scrolling through song select fires one message per frame; the
desktop tool waits 300 ms on a stable checksum before doing anything, and a
remote client should be at least as polite.

### A Python client that featurises locally

Sends 3 KB of JSON instead of a 40 KB file, and keeps the beatmap on the
player's machine.

```python
import httpx
from osumapmatcher.features import FEATURE_VERSION, featurize, variant_for_mods

API = "https://api.example.com/api/v1"

def similar(osu_bytes: bytes, mods: str = "", **options) -> dict:
    # featurise with the CANONICAL mods of the variant, not the raw ones:
    # the DT corpus was built with "DT", so a vector computed with "HDDT"
    # would be compared against it with a silent bias
    variant = variant_for_mods(mods)
    features = featurize(osu_bytes, None if variant == "nomod" else variant)
    if features is None:
        raise ValueError("this map cannot be featurised")

    response = httpx.post(f"{API}/similar", json={
        "features": features,
        "feature_version": FEATURE_VERSION,
        "featurized_with": "" if variant == "nomod" else variant,
        "variant": variant,
        "mods": mods,
        **options,
    }, headers={"User-Agent": "osu-mapmatcher/0.1"}, timeout=15.0)
    response.raise_for_status()
    return response.json()
```

Check compatibility once at startup rather than discovering it on a 409:

```python
meta = httpx.get(f"{API}/meta").json()
if meta["feature_version"] != FEATURE_VERSION:
    use_local_index_instead()
```

### If the instance sits behind Cloudflare

Cloudflare's bot protection answers **403** to a few well-known default user
agents. Measured on a live instance: `Python-urllib/3.12` is refused on every
request, while `curl`, a browser, and an explicit `User-Agent: osu-mapmatcher/0.1` all
pass. Whatever you write, **send a User-Agent**.

That 403 comes from the edge, not from the API, so it has no `detail` field and
no `feature_version`. Read it as "this client identifies badly", not as an API
error — and do not treat it as a reason to fall back to local, since retrying
with a proper header works.

### Falling back to local

The rule that matters: **a remote failure must never be a dead end.** Treat
429, 503, a timeout, and a `feature_version` mismatch the same way — switch to
the local index for the rest of the session and say so in the interface. A tool
that stops working because someone else's server went down is the exact failure
this project was written to avoid.
