# Publication v2 — safe historical rebuild and final cutover

The original `measurements/main` GitHub Pages content, historical
`publisher/state/*.json`, and R2 production objects are **not modified**.
The new architecture lives on `publication-v2`, which must not become the
GitHub Pages source until the new archive is complete.

## Approved design

- Scientific authority: MILGRAU `milgraumaxxing` L0/L1/L2, its configuration,
  station timezone, continuous scientific sessions and plot renderers.
- New web products: only station-local 00/06/12/18 windows from L1, with
  WebP output. No scientific NetCDF is split/merged and no L2 plots are
  published at this stage. No Mean RCS overview tab.
- Temporary files: shared workspace `.cache/measurements/`, isolated from
  `.cache/era5`, weather, radiosonde and other MILGRAU caches.
- Permanent R2 content: hash-addressed, staging-only
  `staging/publication-v2/station/YYYY/MM/YYYYMMDD/SHA256/name.webp`.
- Permanent state: Git-owned `publisher/state-v2/station/YYYY.json`, solely
  for remotely verified assets, incremental between local runs. No old
  state-v1/saam/sapm/sant import or fallback.
- Frontend: continue using the existing calendar, dashboard appearance and
  quicklook channel/altitude/period controls. HTML is rebuilt from persistent
  state even with empty cache. No live production switch is automated.

## Runtime sequence per batch

1. Inspect L1 session inventory, render selected local windows to
   `.cache/measurements/`, and validate sidecars with SHA-256 hashes.
2. Run `publish-v2.py --dry-run` to inspect candidates.
3. Explicitly authorize staging with `--confirm-staging-upload` and bucket.
   Verify **downloaded R2 object bytes** against local SHA-256.
4. Persist new/updated window entries in `publisher/state-v2/`, commit only
   these files, push to `origin/publication-v2`, and compare remote SHA.
5. Only after all previous steps succeed, delete the verified WEBPs/sidecars
   of that batch. Keep the rest of `.cache` untouched. On any failure, retain
   files and retry safely; use `--keep-cache` for debugging.
6. Build the unchanged-layout site locally with `build-site-v2.py` from
   Git state; inspect day coverage, asset URLs, sessions and gaps.

## Historical scientific reprocessing

Reprocessing of raw measurements is not implemented by the website publisher.
Run the MILGRAU L0/L1/L2/QA processing with explicit historical checkpoints
and completion criteria, generating and publishing in manageable batches.
The source `01-data` is never deleted by the publisher.

Do **not** empty `measurements/main`, alter its old manifest, delete
production R2 files, or switch GitHub Pages while coverage is incomplete.
When all historical scientific and website outputs are audited, prepare a
separately reviewed cutover (including remote staging-to-live object promotion,
public URLs, full manifest, and a rollback snapshot). Only after verified live
operation should obsolete production R2 files be considered for deletion.

The former `03-site-products/` directory is not reused. Its existing content
is not automatically removed by the new pipeline.
