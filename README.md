# SPU Lidar measurements — optional publisher

The experimental publisher is isolated on **measurements/publication-v2**.
**It does not change measurements/main, the live GitHub Pages site, historical
year-root R2 objects, or MILGRAU scientific NetCDFs.**

## Optional installation (one environment, no extra config)

The measurements package installs the single `milgrau-publish` command.
MILGRAU stays the owner of scientific sessions, the Level 1 NetCDF schema,
configuration, image renderer, and station timezone. No publication settings
are added to `milgrau/config.yaml`.

```bash
cd ~/milgrau
git -C milgrau switch main
git -C measurements switch publication-v2
git -C measurements pull --ff-only origin publication-v2
python -m pip install -e "./milgrau[web]"
python -m pip install -e ./measurements
milgrau-publish --help
```

Install the optional measurements package in the **same active venv** as
MILGRAU. The console script works from any directory after editable install.

The publisher reads `~/milgrau/config.yaml` and the existing private
`credentials.py` at either `~/milgrau/credentials.py` or
`~/milgrau/milgrau/credentials.py`, in that order. It recognizes:

```text
GITHUB_USER, GITHUB_TOKEN,
R2_BUCKET_NAME, R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_PUBLIC_URL
```

Environment variables with those names override file values. `credentials.py`
must stay outside Git and must never be shared. The Git token is passed to
Git only through process environment for HTTPS remotes; it is not embedded
in repository URLs or command arguments. Existing SSH Git authentication
also works.

## Everyday command

```bash
milgrau-publish
```

This discovers available local L1s, rejects malformed L1 timestamps with an
audit, checks the Git-owned state fingerprints, creates only necessary WEBPs
using MILGRAU, uploads them to R2, downloads the remote bytes to verify SHA-256,
and updates year state, day HTMLs and calendar manifest together with **one
Git commit and push** on `publication-v2`. It only removes temporary local
WEBPs/sidecars after GitHub confirms the committed branch HEAD.

The scientific data in `02-processed_data/` are **never altered**. Missing
local sessions or days are **never interpreted as deletions**.

WebPs are stored directly under:

```text
spu/YYYY/MM/YYYYMMDD/rcs_YYYYMMDD_spu_HH_CHANNEL_ALTkm.webp
```

There are no hash subfolders. SHA-256 stays in `publisher/state-v2` and
only image URLs receive `?v=SHA256` to distinguish cache versions.
Day-page URLs remain `YYYY/MM/YYYYMMDD/index.html`.

The HTML calendar file normally stays untouched: its data come from the
updated `measurements.json`. Before cutover, **production main/Pages and
legacy R2 objects remain unchanged**.

## All supported operations

| Command | Action |
| --- | --- |
| `milgrau-publish` | Incremental sync, R2 + state + HTML + Git |
| `milgrau-publish --date 20260731` | Sync only the specified local day |
| `milgrau-publish --dry-run` | Plan images and pending windows without writes |
| `milgrau-publish status` | Count local L1s, staged windows, state and blocked dates |
| `milgrau-publish images 20260731` | Render only local WEBPs and diagnostic preview |
| `milgrau-publish html` | Rebuild and commit/push site from verified state only |
| `milgrau-publish hide 20260731` | Remove day from calendar and HTML; keep R2 bytes |
| `milgrau-publish remove 20260731` | Preview all objects affected; no mutation |
| `milgrau-publish remove 20260731 --confirm` | Back up, remove day from Git, then delete exact verified R2 objects |
| `milgrau-publish restore 20260731` | Unhide or restore backed-up objects and day state |

`images` generates a local, disposable diagnostic preview under
`.cache/measurements/_preview-v2` directly from verified cached WEBPs and
sidecar JSON files; it does not require or create the old duplicated
`_state-v2` planning files. Scientific NetCDFs are never modified.

`hide` and `remove` write persistent controls under
`publisher/state-v2/_controls/spu.json` so the next ordinary sync will
**not republish the blocked day**. `restore` explicitly removes that block.

A real `remove` first verifies every R2 object matches the Git-published
SHA-256 and stores a byte-verified backup under
`_publication_v2_backups/<SHA256>/spu/YYYY/MM/YYYYMMDD/<filename>`.
Next it removes the day from the Git state and generated HTMLs and confirms
the remote commit. Only then does it delete the **exact** old R2 keys.
If cleanup fails, rerunning the same confirmed command resumes pending
deletions from the tombstone. It never deletes a folder/prefix blindly, or
touches historical `YYYY/` keys.

A previously published image is only overwritten when the existing R2 bytes
exactly match the previous verified Git state; it gets a verified backup
first. A failed known Git push restores the previous local commit and remote
image bytes. If Git remote outcome is ambiguous, the publisher fails closed
and needs manual reconciliation. Temporary products and the last-run journal
are retained in `.cache/measurements/` for safe re-execution.

## Safety and deployment

Publishing requires a clean checkout on **publication-v2**, with an
`origin` pointing to `spulidar/measurements` and synchronized with GitHub.
Do not enable GitHub Pages from this branch or merge it into `main`
until history, URLs, site content and an appropriate rollback have been
reviewed separately.

The four former root CLI scripts (`update-site.py`,
`publish-v2.py`, `build-site-v2.py`, `preview-site.py`) were removed.
Their useful internals remain as focused modules. Obsolete R2 credential
discovery, remote-verification wrappers and the duplicated local
`_state-v2` cache writer have also been removed; `milgrau-publish` is the
only supported operational entrypoint.

The modules in `publisher/` are **not** independent commands: `cli` and
`pipeline` coordinate execution; `windows` and `staging` use MILGRAU
to discover/render; `v2` validates local sidecars; `r2_staging`,
`published`, `publication` and `history` handle publication; `render`,
`site_v2`, `site_export` and `preview` handle generated pages and
diagnostic preview. They remain separate to preserve testable responsibilities.

## Quick checks

```bash
milgrau-publish status
milgrau-publish --dry-run
python -m pytest -q tests
```

The complete CI tests are offline and must never perform real R2 uploads
or write to production GitHub branches.
