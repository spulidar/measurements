# SPU-Lidar measurements — publication v2

**The live site remains unchanged.** `measurements/main`, GitHub Pages,
its historical HTML and `measurements.json`, and the existing R2 objects are
untouched by the v2 development commands. Work only on `publication-v2`.
The visual frontend remains the existing daily dashboard and year calendar,
except that **Mean RCS / GlobalMeanRCS is no longer generated or displayed**.
A possible Level 2 page is future work, not implemented.

## One MILGRAU workspace, one Python environment

```text
workspace/
├── config.yaml                # authoritative MILGRAU configuration
├── station.yaml
├── 01-data/                   # raw data
├── 02-processed_data/         # scientific L0/L1/L2, never edited by publisher
├── .cache/
│   ├── era5/                  # other MILGRAU caches are untouched
│   ├── weather/
│   ├── radiosonde/
│   └── measurements/          # temporary publication staging only
│       ├── spu/YYYY/MM/YYYYMMDD/
│       │   ├── rcs_YYYYMMDD_spu_HH_CHANNEL_ALTkm.webp
│       │   └── YYYYMMDD_spu_HH.json
│       ├── _state-v2/         # disposable local planning state for legacy preview
│       ├── _preview-v2/       # optional diagnostic preview, NOT the live layout
│       └── _site-v2/          # generated local copy of the EXISTING website
├── milgrau/                   # checkout main
└── measurements/              # checkout publication-v2
    ├── publisher/state-v2/spu/YYYY.json # PERSISTENT published windows, Git-tracked
    ├── measurements.json      # old LIVE catalog, frozen until cutover
    └── ql-measurement-calendar.html # existing frontend, unchanged
```

Install from current MILGRAU, using the same environment in both checkouts:

```bash
cd ~/milgrau/milgrau
pip install -e ".[web]"
cd ../measurements
git switch publication-v2
git pull --ff-only origin publication-v2
```

`--milgrau-config ../config.yaml` is always available. The CLI reads the
station/timezone and scientific data paths from MILGRAU's own loader and
`milgrau.io.paths`. There is no `measurements/config.yaml`, requirements
file, or separate Python environment.

## 1. Generate only L1 quicklook images in the temporary cache

```bash
python update-site.py --stage-only --milgrau-config ../config.yaml --publish-date 20240913
python update-site.py --stage-only --milgrau-config ../config.yaml --publish-year 2024
python update-site.py --stage-only --milgrau-config ../config.yaml --dry-run --publish-date 20240913
```

This uses the canonical whole-session L1 NetCDFs and the MILGRAU generic
renderer. Only publication-time views are sliced in memory. Four possible
windows: 00–06, 06–12, 12–18, 18–24, in the station's local civil timezone.
Science NetCDFs are never split, modified, merged, regridded or reprocessed by
`measurements`; time gaps and scientific source IDs are preserved.
No L2 graphics or mean RCS charts are generated.

The per-window sidecar lists the exact inputs, fingerprint and SHA-256 of
every WEBP. The disposable `.cache/measurements/_state-v2` is **only** for the
older local diagnostic preview and must never be treated as the published
state. The Git-tracked published state is a different directory.

## Handling older L1 files with invalid time coordinates

`update-site.py --stage-only` without a selector scans the entire historic
L1 catalog. Scientific NetCDF `time` coordinates must be strictly increasing:
duplicate or backward timestamps fail *by default*. The publisher never
reorders, deduplicates, modifies, or removes scientific source profiles.

Use a local civil date/year to avoid touching unrelated historical sessions:

```bash
python update-site.py --stage-only --milgrau-config ../config.yaml --publish-date 20240913
python update-site.py --stage-only --milgrau-config ../config.yaml --publish-year 2024
```

Date/year selection filters canonical session IDs conservatively (two-day
boundary guard), **before** opening their scientific NetCDF coordinates. Exact
window selection still checks the real `time` samples.

For an exploratory complete-catalog run where L1 repairs are still pending,
you may explicitly opt in to excluding problematic scientific L1:

```bash
python update-site.py --stage-only --milgrau-config ../config.yaml --skip-invalid-l1
```

Every excluded session is logged and listed in
`.cache/measurements/_audit/invalid-l1-all.json`; year/date-specific reports
are named after the requested scope. Dry-run mode only logs exclusions and
does not write the report. Existing cached figures that reference excluded
scientific sessions are rejected rather than silently approved. Fix source
data in MILGRAU and restage for scientific completeness before the final
historical publishing cutover. This opt-in is not a scientific correction.

## 2. Stage to R2, verify, persist state to GitHub, clean cache

**Never use these commands for production cutover.** The publishing command
is intentionally restricted to the immutable
`spu/YYYY/MM/YYYYMMDD/SHA256/filename.webp` key namespace.
The legacy live objects use top-level `YYYY/` directories; the new v2 objects
use the independent top-level `spu/` directory. Use an explicit bucket and
confirmation flag. A shared bucket is possible because these prefixes are
disjoint, but a separate test bucket is recommended for the first pilot.

First see which windows are eligible, with no credentials or network calls:

```bash
python publish-v2.py --milgrau-config ../config.yaml \
  --staging-bucket YOUR_R2_STAGING_BUCKET --dry-run
```

Only when ready to **actually upload** (this command makes R2 and GitHub
writes):

```bash
export R2_ENDPOINT="https://<account>.r2.cloudflarestorage.com"
export R2_ACCESS_KEY="..."
export R2_SECRET_KEY="..."
python publish-v2.py --milgrau-config ../config.yaml \
  --staging-bucket YOUR_R2_STAGING_BUCKET \
  --batch-size 12 --confirm-staging-upload
```

The existing private, Git-ignored `credentials.py` is also supported for
R2 credentials. Never commit tokens.

After a one-window pilot has been verified, `--all` automatically publishes
every remaining cached window in safe batches. Each batch commits its own
yearly state updates, verifies remote GitHub HEAD and frees its WEBPs before
continuing. If interrupted, rerun the same command; already-published windows
are retained in Git and no longer require a local copy.

```bash
python publish-v2.py --milgrau-config ../config.yaml \
  --staging-bucket YOUR_R2_STAGING_BUCKET \
  --batch-size 24 --all --confirm-staging-upload
```

`--all` cannot be combined with `--keep-cache`. No automatic R2 deletion,
object promotion or production GitHub Pages publishing is performed.


For **each** window in a batch:
1. Reject files without a matching sidecar, attested hashes and canonical identity.
2. Upload missing or mismatched objects to immutable hash-addressed R2 keys inside the new `spu/` namespace.
3. **Download the remote bytes again** and check size plus SHA-256.
4. Incrementally merge verified records into
   `publisher/state-v2/spu/YYYY.json` in the measurements Git checkout.
5. Commit only changed state files, push **only** to
   `origin/publication-v2`, and check that the remote branch SHA matches.
6. Finally delete **only** the verified WEBPs and sidecars belonging to these
   windows from `.cache/measurements/`. Never clear the whole `.cache`.
   Other MILGRAU caches and the scientific data are not touched.

If the R2 upload, download verification, Git commit/push or remote SHA
confirmation fails, the WEBPs stay in cache. The operation is restartable:
immutable R2 objects with matching bytes are reused and previously registered
windows survive the removal of local WEBPs. If a Git push fails *after a local
commit*, reconcile/push the branch before retrying; do not force-push.
`--keep-cache` disables the cleanup when debugging.

New state starts **empty** (no schema-v1 history import). The v2 state is
persisted in the Git repository only when the corresponding R2 objects were
actually verified. The current production site is neither modified nor used
as fallback by the new pipeline. There is **no R2 deletion operation**.

## 3. Rebuild the same site locally from Git state alone

After verified publication, when the cache WEBPs have been removed:

```bash
python build-site-v2.py --milgrau-config ../config.yaml \
  --r2-public-base https://YOUR_STAGING_PUBLIC_R2_BASE
python -m http.server 8765 --bind 127.0.0.1 --directory ../.cache/measurements/_site-v2
```

Open `http://127.0.0.1:8765/ql-measurement-calendar.html`. The calendar and
daily page appearance/selection controls are reused from the current site.
Only the obsolete mean RCS tab is hidden. The builder reads **only** Git-owned
`publisher/state-v2`; `03-site-products`, old state-v1, and cache WEBPs
are not required. HTML and manifest generation remain local and cannot
update `measurements/main` or the production Pages site.

### Put generated HTML and measurements.json into publication-v2 (new)

The R2 bucket needs a **public HTTPS base URL** (custom domain or configured
public R2 domain) that serves object keys starting with
`spu/YYYY/MM/YYYYMMDD/`. The R2 S3 API endpoint is **not** the public URL.
Test access to one object through that public domain before export.

Once all required windows are recorded in Git state:

```bash
python build-site-v2.py --milgrau-config ../config.yaml \
  --r2-public-base https://YOUR_PUBLIC_STAGING_R2_DOMAIN \
  --write-branch
```

This first regenerates `.cache/measurements/_site-v2`, then installs only
validated `YYYY/MM/YYYYMMDD/index.html` files and root `measurements.json`
into the **clean, synchronized** `measurements/publication-v2` working tree.
It refuses an empty state/catalog, prevents overwriting unrelated HTML, and
will only remove obsolete daily pages previously produced by this renderer.
It does **not** change `ql-measurement-calendar.html` or push/deploy anything.

Preview the generated local site (the calendar uses relative links there):

```bash
python -m http.server 8765 --bind 127.0.0.1 \
  --directory ../.cache/measurements/_site-v2
```

Check `http://127.0.0.1:8765/ql-measurement-calendar.html`, plus a few
quicklooks at different dates/channels. Then commit **only** these generated
site files and push to the development branch:

```bash
git status --short
git add -- measurements.json
git add -A -- ':(glob)[0-9][0-9][0-9][0-9]/**/index.html'
git diff --cached --stat
git commit -m "Build publication-v2 HTML and calendar manifest from verified state"
git push origin publication-v2
```

Do not merge to `main` or change the repository's GitHub Pages publishing
source yet. If new windows are published later, rerun `build-site-v2.py
--write-branch` after completing the preceding Git commit/push. Rebuild
always uses the persistent state from Git, **not** deleted local WEBPs.

The earlier separate `preview-site.py` remains available as an optional
diagnostic viewer for still-cached images, **not** the intended new frontend.

## Full historical reprocessing and final cutover — not executed

Reprocess historical `01-data` through MILGRAU's L0/L1/L2/QA separately.
Stage and upload WEBPs month-by-month or year-by-year, freeing disk space
between verified batches. Keep the live website and its existing R2 content
online until *all* new historical windows, asset coverage, time gaps, and
external URLs have been accepted. Only then prepare a deliberate final
production deployment and separately authorized old-R2 cleanup.
The public site must never be switched while the new Git-owned catalogue is
empty or incomplete. Historical legacy `saam/sapm/sant` is NOT migrated into
state v2; old production remains only as rollback during the transition.

The old `03-site-products/` directory from previous tests is **not**
automatically deleted or migrated. Review it manually after the new cache
pipeline has been verified; no command here can remove it.

## Branch reset for historical reconstruction

The `publication-v2` branch deliberately omits the tracked `2012/` to `2026/`
historical HTML directories, old root `measurements.json`, legacy
`publisher/state/`, and obsolete schema-v1 publisher modules and tests. The
new `publisher/state-v2/spu/` is empty until the first verified staging R2
publication is recorded. These removals affect only the development branch;
`main`, GitHub Pages and production R2 are unchanged. The original calendar
source and the daily dashboard appearance are preserved. Generate a fresh
local manifest/site using `build-site-v2.py`; with empty state it produces an
empty `days` mapping, without consulting the legacy archive.

The earlier R2 prefix `staging/publication-v2/spu/...` is not used for
new uploads. No objects under the earlier prefix are automatically moved or
deleted. This path change was made while the Git-tracked v2 state had no
published year records. Reconcile any untracked R2 uploads from older runs
separately rather than deleting them blindly.
