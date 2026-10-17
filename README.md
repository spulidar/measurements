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
├── milgrau/                   # checkout milgraumaxxing
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

## 2. Stage to R2, verify, persist state to GitHub, clean cache

**Never use these commands for production cutover.** The publishing command
is intentionally restricted to the immutable
`staging/publication-v2/spu/YYYY/MM/YYYYMMDD/SHA256/filename.webp` key
namespace. Use an explicit bucket and confirmation flag. The bucket may be
shared with production *only because these staging keys are disjoint*;
a separate staging bucket is preferable.

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

For **each** window in a batch:
1. Reject files without a matching sidecar, attested hashes and canonical identity.
2. Upload missing or mismatched objects to immutable hash-addressed R2 staging keys.
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
