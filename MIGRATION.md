# Session-aware publication migration

This change adapts `spulidar/measurements` to MILGRAU `session-refactor` without migrating existing R2 objects or historical state.

## What changes

- publication windows `00`, `06`, `12`, `18` are owned exclusively by this repository;
- their full identity is `YYYYMMDD_station_HH` and is called `publication_window_id`;
- MILGRAU scientific session IDs are read through `milgrau.io.paths` and are never recreated here;
- L1/L2 NetCDFs are read directly from `02-processed_data`;
- website rendering happens in `03-site-products`;
- only staged public WEBPs can be uploaded;
- multiple scientific sessions may contribute to one website plot, but are concatenated only in memory and retain `source_session_id`;
- real gaps are not resampled or filled; MILGRAU's generic quicklook renderer displays them as missing data;
- current public/R2 filenames and keys are retained;
- state schema 1 is retained.

## Existing checkout

Apply the changed source files over the current `measurements` checkout. **Do not delete**:

```text
publisher/state/
YYYY/                 # historical redirects/pages
measurements.json     # it will be regenerated as needed
```

Create the new staging directory beside the repos when the publisher first runs:

```text
03-site-products/
```

## First controlled test

Use a single known date and do not push Git initially:

```bash
python -m pytest -q
python update-site.py --publish-date 20250510 --stage-only
python update-site.py --publish-date 20250510 --skip-stage --no-push
```

Inspect:

```text
../03-site-products/spu/2025/05/20250510/
```

Each publication window has a JSON sidecar showing the contributing scientific session IDs and whether more than one session was concatenated for visualization.

## Public compatibility

A new staged file such as:

```text
03-site-products/spu/2025/05/20250510/rcs_20250510_spu_18_532nm_AN_15km.webp
```

is still uploaded as:

```text
2025/05/20250510/rcs_20250510_spu_18_532nm_AN_15km.webp
```

No `spu/` prefix is inserted into the established public R2 key, so existing HTML/state links stay valid.
