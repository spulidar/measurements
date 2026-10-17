# SPU-Lidar measurements publisher

This repository owns the **public website layout and publication state** for SPU-Lidar. Scientific acquisition identity belongs to MILGRAU.

## Identity boundary

MILGRAU `session-refactor` stores one continuous scientific acquisition as a session such as:

```text
spu_20250511-0012Z_20250511-0737Z
```

A session can cross midnight or any website boundary. `measurements` independently owns four station-local publication windows:

```text
00–06  -> YYYYMMDD_spu_00
06–12  -> YYYYMMDD_spu_06
12–18  -> YYYYMMDD_spu_12
18–24  -> YYYYMMDD_spu_18
```

These identifiers are `publication_window_id` values. They are **not session IDs**.

Example in São Paulo local time:

```text
scientific acquisition: 10/05 21:12 -> 11/05 04:37
MILGRAU session:         spu_20250511-0012Z_20250511-0737Z
website views:           20250510_spu_18 (21:12–24:00 local)
                         20250511_spu_00 (00:00–04:37 local)
```

No scientific NetCDF is split. The website only slices the data in memory for visualization.

## Directory boundary

```text
workspace/
├── milgrau/
├── measurements/
├── 02-processed_data/       # MILGRAU scientific NetCDF products only
└── 03-site-products/        # website-only rendered WEBPs + provenance sidecars
    └── spu/YYYY/MM/YYYYMMDD/
```

`measurements` no longer recursively scans arbitrary scientific WEBPs under `02-processed_data`. Only validated WEBPs in `03-site-products` are eligible for R2 upload.

The public R2 key remains compatible with the existing site:

```text
YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_532nm_AN_15km.webp
```

so no historical object migration is required.

## How publication works

For each selected local publication window the publisher:

1. discovers canonical MILGRAU `SESSION_L1.nc` and optional `SESSION_L2.nc` files;
2. finds every scientific session intersecting the local window;
3. reads the exact L1 time span in `[window_start, window_end)`;
4. concatenates multiple session slices **only in memory for visualization**;
5. preserves the original `source_session_id` along the time coordinate and keeps real time gaps unchanged;
6. calls MILGRAU's generic `plot_quicklook` and `plot_global_mean_rcs` renderers;
7. writes only public WEBPs and a provenance JSON sidecar to `03-site-products`;
8. uploads those staged WEBPs to the established R2 paths;
9. updates the existing schema-v1 publication state and daily HTML.

L2 products are read and their intersecting block support is recorded in the sidecar. The current public site does **not** synthesize a windowed L2 optical mean, because the present MILGRAU optical-profile renderer consumes session-aggregate variables. Recomputing those aggregates in this repository would duplicate scientific logic. When MILGRAU exposes a generic window-view L2 renderer, `measurements` can call it without changing session identity or state ownership.

## Environment

Until MILGRAU adds the planned `web` extra, install both in the **same environment**:

```bash
cd workspace/milgrau
pip install -e .
pip install -r ../measurements/requirements.txt
```

There is no need for two virtual environments.

## Normal use

Publish one local day after processing/reprocessing it in MILGRAU:

```bash
cd workspace/measurements
python update-site.py --publish-date 20250510 --no-push
```

Build website staging only:

```bash
python update-site.py --publish-date 20250510 --stage-only
```

Publish already staged assets without rerendering:

```bash
python update-site.py --publish-date 20250510 --skip-stage --no-push
```

Force regeneration of selected website assets:

```bash
python update-site.py --publish-date 20250510 --force-site-products --no-push
```

Historical state/bootstrap/cleanup commands remain available:

```bash
python update-site.py --validate-state
python update-site.py --bootstrap-state-from-r2 --html-only --no-push
python update-site.py --cleanup-report 2024
```

## Historical compatibility

The committed `publisher/state/YYYY.json` files stay at schema version 1. The legacy JSON field named `canonical` is retained as a storage key so the historical archive does not require migration; new code treats each entry as a publication-window record and adds `publication_window_id` plus source-session provenance when a window is republished.
