# Publication v2 — migration and future cutover plan

This document describes the planned transition. **No migration, reprocessing,
R2 upload/delete, or production site changes are authorized by this file.**

## Production freeze

- `spulidar/measurements/main`: existing GitHub Pages, legacy dashboard,
  manifest, `publisher/state/*.json` and current R2 references remain frozen.
- `spulidar/measurements/publication-v2`: development of a new clean
  site-publication architecture, with no executable publishing/deletion path.
- `spulidar/milgrau/milgraumaxxing`: the independent scientific source of
  truth; it is **not** a publication engine.

The old schema-v1 and historic `saam/sapm/sant` system is not migrated
incrementally. The new dataset is generated from reprocessed history and
published as a whole only after validation. The new v2 model uses
`days[YYYYMMDD].publication_windows[HH]`.

## Development environment

Use one Python 3.12+ environment, installed from MILGRAU:

```bash
cd workspace/milgrau
pip install -e ".[dev,explorer,web]"
cd ../measurements
python -m pytest -q
python update-site.py --stage-only --publish-date 20250510
```

The `measurements` v2 checkout has **no** separate config.yaml or
requirements.txt. MILGRAU `config.yaml` and `station.yaml` are loaded via
MILGRAU APIs. Only local products under
`workspace/03-site-products/station/YYYY/MM/YYYYMMDD/` and local
`03-site-products/_state-v2/station/YYYY.json` are generated.

Local WEBPs require a valid same-window sidecar with listed filenames and
hashes; orphan scientific or manual WEBPs cannot enter the v2 catalog. Final
R2 key design is `station/YYYY/MM/YYYYMMDD/asset.webp`, **not** the old
unprefixed path. This is a future interface, not a cloud migration.

## Year-by-year historical reprocessing (future, not now)

For each year, with checkpointed scientific runs:

1. Inventory immutable `01-data/` raw acquisition content and station profile.
2. Generate L0 using LIBIDS, L1 using LIPANCORA, L2 using LEBEAR.
3. Review QA, uncertainty semantics, support masks and provenance.
4. Inventory continuous scientific sessions, L1/L2 availability and temporal gaps.
5. Generate four station-local publication views wherever samples exist,
   never cutting a NetCDF on midnight or 6-hour boundaries.
6. Validate sidecars, hashes and local schema-v2 year state.
7. Record counts: scientific sessions, local days, publication windows,
   windows with multiple sessions, gaps, L1/L2 support, staging failures.

Do not delete `01-data/`. Any rebuild/delete of
`02-processed_data/` or `03-site-products/` needs a separately approved
command and full backup/verification plan.

## Separate cutover decision

Only after the full historical archive has passed scientific and publication
acceptance:

1. Generate and test new HTML/manifest against local schema v2.
2. Upload to an **isolated** R2 bucket/prefix, not production.
3. Audit URL resolution, complete temporal coverage and asset inventories.
4. Authorize and execute the production switch separately.
5. Observe live behavior and validate rollback readiness.
6. Remove retired production R2 objects and legacy site code **only after**
   the replacement has been verified, in a dedicated cleanup change.

Never run old `--skip-stage`, `--bootstrap-state-from-r2`, `--prune-r2`,
`--cleanup-report`, or automatic Git push instructions against the new
development pipeline.
