# SPU-Lidar measurements — publication v2 development

**Production isolation:** `main` remains the unchanged live site. All new
publication development is isolated on `publication-v2`, created from the
existing `main` commit. Never run this branch as GitHub Pages production;
do not merge it or switch the Pages source before the explicit cutover.

## Scientific/publication responsibilities

- **MILGRAU** owns scientific configuration/station metadata, L0, L1, L2, QA,
  continuous scientific session IDs and all generic figure renderers.
- **measurements** owns only four station-local public views (00–06, 06–12,
  12–18, 18–24), website-only WEBPs, provenance, public state and eventual
  website/cloud publication.

One session ID such as `spu_20250511-0012Z_20250511-0737Z` remains a
single scientific NetCDF from 10 May 21:12 to 11 May 04:37 São Paulo local
time. It contributes to `20250510_spu_18` and `20250511_spu_00`, without
splitting the file. Conversely a publication window can receive slices from
multiple sessions. Slices are concatenated only **in memory for plotting**;
`source_session_id` survives, gaps are retained, and duplicate timestamps or
incompatible altitude/channel grids fail instead of silently averaging,
deduplicating, interpolating or regridding.

Only canonical MILGRAU L1 NetCDF is discovered; SCC derivatives do not create
extra sessions. L2 files are inspected for intersecting block coverage and
provenance, but this publisher **does not** compute window-specific scientific
L2 means or render session-aggregate L2 graphics as though window-specific.

## Workspace and installation

```text
workspace/
├── config.yaml               # MILGRAU recipe
├── station.yaml              # MILGRAU station catalog
├── 01-data/                  # immutable raw acquisitions
├── 02-processed_data/        # L0/L1/L2 NetCDF, MILGRAU only
├── 03-site-products/         # generated public WEBPs, sidecars and local v2 state
├── milgrau/                  # checkout milgraumaxxing
└── measurements/             # checkout publication-v2
```

Create one Python 3.12+ environment from the current MILGRAU checkout:

```bash
cd workspace/milgrau
pip install -e ".[web]"
# For complete development: pip install -e ".[dev,explorer,web]"
cd ../measurements
python update-site.py --stage-only --publish-date 20250510
```

The `web` extra supplies the optional `boto3` dependency. MILGRAU's core
already supplies xarray, matplotlib, PyYAML etc. No separate
`measurements/config.yaml`, `requirements.txt` or virtualenv is used.

`--milgrau-config /path/to/config.yaml` overrides the config file search;
otherwise `MILGRAU_CONFIG`, then `config.yaml` in the cwd or shared workspace
is used. MILGRAU's official loader and path functions supply the scientific
root and station/timezone/visualization recipe. Relative paths (including
logos) are resolved using MILGRAU's official logo-path resolver. The
workspace root is preferred; if the configured images all reside in the
installed MILGRAU checkout's `img/`, its package root is used explicitly.
Mixed/missing logo sets cause an error instead of being silently omitted. `03-site-products` is a publication-only sibling
of the configured scientific `02-processed_data`.

## Strict local stage-only pipeline

```bash
cd workspace/measurements
python update-site.py --stage-only --publish-date 20250511
python update-site.py --stage-only --publish-year 2025
python update-site.py --stage-only --dry-run --publish-date 20250511
```

There is intentionally **no** upload/delete R2, production HTML generation,
production state bootstrap/mutation, Git auto-push or --prune-r2 path in the
new entrypoint. The `--stage-only` flag is mandatory; unrecognized legacy
options fail argument parsing.

```text
03-site-products/
├── spu/2025/05/20250510/
│   ├── rcs_20250510_spu_18_532nm_AN_15km.webp
│   └── 20250510_spu_18.json
├── spu/2025/05/20250511/
│   ├── rcs_20250511_spu_00_532nm_AN_15km.webp
│   └── 20250511_spu_00.json
└── _state-v2/spu/2025.json
```

The sidecar must attest `publication_window_id`, source sessions, exact L1
sources, intersecting L2 sources, source fingerprint,
`scientific_sessions_merged=false`,
`scientific_netcdf_split=false`, visual-concatenation flag,
explicit listed assets and their SHA-256 hashes. Sidecar validation rejects
wrong station/day/path, absent or modified assets and foreign source
identities. Unlisted WEBPs are never eligible for a future upload.

A staging cache is invalidated by source L1/L2 file size/mtime, renderer
implementation bytes, adapter code, MILGRAU config, station config and selected
logo bytes, without requiring a package version bump. A valid sidecar plus
verified asset hashes permits skipping an unchanged render.

The local state is schema v2 with `days[YYYYMMDD].publication_windows[HH]`,
not `canonical`. Future R2 layout will be
`spu/YYYY/MM/YYYYMMDD/<asset>`, but **no cloud publication happens now**.



## Local website preview (no production publishing)

After a real `--stage-only` run has created the WEBPs, sidecars and state-v2,
build a separate browser preview:

```bash
cd ~/milgrau/measurements
python preview-site.py --milgrau-config ../config.yaml
python -m http.server 8765 --bind 127.0.0.1 --directory ../03-site-products
```

Open **http://127.0.0.1:8765/_preview-v2/** in your browser. Stop the server
with Ctrl+C. It is bound to the loopback interface, not exposed to the network.

The preview lives only under `03-site-products/_preview-v2/` and includes
`index.html`, `styles.css`, `app.js`, and a generated `manifest.json`.
It displays a month calendar, the four local publication windows, a channel
drop-down, available WEBP figures and their contributing scientific session IDs.
Clicking a figure opens its full-resolution WEBP.

The preview builder accepts **only validated state-v2 and sidecar-attested
assets**. It fails if a state file is stale, an attested WEBP hash changed, or
expected state is missing; rerun `update-site.py --stage-only` before rebuilding
the preview. This first preview intentionally shows static images, **not**
interactive NetCDF plots. It has no CDN, R2 connection, external CSS/JS,
production HTML mutation or GitHub Pages deployment. The public site remains
unchanged on `measurements/main`.

For a date you have already tested (13 September 2024):

```bash
python update-site.py --stage-only --milgrau-config ../config.yaml --publish-date 20240913
python preview-site.py --milgrau-config ../config.yaml
```

The preview calendar will show every locally staged, validated day, not just
the date of the most recent run. To check the specific result in the browser,
use `http://127.0.0.1:8765/_preview-v2/?date=20240913&window=12`.

## Future historical rebuild and cutover (not part of this phase)

Process `01-data` through L0/L1/L2/QA by year with checkpoints recording
scientific session counts, covered local days, publication windows, multi-session
windows, gaps, L1/L2 coverage and staging failures. Rebuild
`02-processed_data` and `03-site-products` only after separate authorization.
Validate the full new archive, test the new HTML/manifest in staging, then
publish to an isolated R2 bucket/prefix. Cut over only after coverage
verification; delete obsolete R2 objects and legacy code only **after**
production has been validated. The old site remains online and frozen
throughout this work.

Legacy publisher modules remain in this development branch as historical
reference only; `update-site.py` does not import or invoke them. They are
not part of the publication-v2 architecture or safety envelope.
