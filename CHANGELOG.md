# Changelog

Dated entries rather than version tags: the index is published as a release
asset that gets replaced in place, so a date says more about what you are
running than a number would.

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

---

## 2026-08-21 — first public release

151,558 ranked and loved beatmaps, five mod variants, the local server, the
hosted API and the tosu overlay.
