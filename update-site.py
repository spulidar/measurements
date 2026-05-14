"""
MILGRAU Suite - Web Publisher

Collects Level 1 graphics (.webp), uploads them to Cloudflare R2,
generates static HTML dashboards per year pointing to the cloud CDN,
and automatically updates the interactive measurement calendar.

Modes:
    python update_site.py
    python update_site.py --html-only
    python update_site.py --sync-missing-uploads
    python update_site.py --no-push
    python update_site.py --dry-run
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import yaml


# =============================================================================
# CONFIGURATION / LOGGER
# =============================================================================

def _validate_config_minimum(config: dict) -> None:
    required_sections = ("directories", "processing")
    missing = [section for section in required_sections if section not in config]

    if missing:
        raise KeyError(
            "Configuration file is missing required section(s): "
            + ", ".join(missing)
        )


def load_config(config_path: str = "config.yaml") -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as file:
        try:
            config = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Error parsing YAML configuration: {exc}") from exc

    if config is None:
        raise RuntimeError(f"Configuration file is empty: {config_path}")

    _validate_config_minimum(config)
    return config


def setup_logger(module_name: str, log_dir: str = "logs") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)

    log_filename = os.path.join(
        log_dir,
        f"{module_name}_run_{datetime.now().strftime('%Y%m%d')}.log",
    )

    logger = logging.getLogger(module_name)
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger


def ensure_directories(*directories: str | Path) -> None:
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class MeasurementData:
    files: list[str] = field(default_factory=list)
    channels: set[str] = field(default_factory=set)
    alts: set[str] = field(default_factory=set)
    has_global_mean: bool = False
    mean_rcs_filename: str = ""


# =============================================================================
# CLOUDFLARE R2
# =============================================================================

def get_cloud_credentials(logger: logging.Logger):
    """Safely loads R2 credentials and initializes the boto3 client."""
    try:
        import credentials

        s3_client = boto3.client(
            "s3",
            endpoint_url=credentials.R2_ENDPOINT,
            aws_access_key_id=credentials.R2_ACCESS_KEY,
            aws_secret_access_key=credentials.R2_SECRET_KEY,
            region_name="auto",
        )

        return (
            s3_client,
            credentials.R2_BUCKET_NAME,
            credentials.R2_PUBLIC_URL.rstrip("/"),
        )

    except ImportError:
        logger.critical(
            "'credentials.py' not found! Please create it with your R2 keys. Exiting."
        )
        sys.exit(1)

    except AttributeError as exc:
        logger.critical(f"Missing required variable in credentials.py: {exc}. Exiting.")
        sys.exit(1)


def upload_to_r2(
    s3_client: Any,
    bucket_name: str,
    local_file_path: str,
    cloud_file_key: str,
    logger: logging.Logger,
) -> bool:
    """Uploads a single file to the Cloudflare R2 Bucket."""
    try:
        s3_client.upload_file(
            local_file_path,
            bucket_name,
            cloud_file_key,
            ExtraArgs={
                "ContentType": "image/webp",
                "CacheControl": "public, max-age=31536000, immutable",
            },
        )
        return True

    except Exception as exc:
        logger.error(
            f"  -> [R2 UPLOAD ERROR] Failed to upload {local_file_path}: {exc}"
        )
        return False


def get_cloud_existing_keys(
    s3_client: Any,
    bucket_name: str,
    logger: logging.Logger,
) -> set[str]:
    """Builds an in-memory index of already uploaded R2 object keys."""
    cloud_existing_keys: set[str] = set()

    logger.info("SYNC MODE ACTIVE: Fetching cloud index...")

    paginator = s3_client.get_paginator("list_objects_v2")

    try:
        for page in paginator.paginate(Bucket=bucket_name):
            for obj in page.get("Contents", []):
                cloud_existing_keys.add(obj["Key"])

        logger.info(
            f"Cloud index built: {len(cloud_existing_keys)} files currently on Cloudflare."
        )

    except Exception as exc:
        logger.error(f"Failed to fetch cloud index: {exc}")
        sys.exit(1)

    return cloud_existing_keys


# =============================================================================
# HTML HELPERS
# =============================================================================

def js_string(value: str) -> str:
    """Safely serializes a Python string as a JavaScript string literal."""
    return json.dumps(value, ensure_ascii=False)


def select_default_channel(valid_channels: list[str]) -> str:
    """Prefer 532nm analog channel when available."""
    return next(
        (
            ch
            for ch in valid_channels
            if "532" in ch and "an" in ch.lower()
        ),
        valid_channels[0] if valid_channels else "",
    )


def select_default_altitude(valid_alts: list[str]) -> str:
    """Prefer 15 km when available."""
    return next(
        (
            alt
            for alt in valid_alts
            if alt in {"15", "15.0"}
        ),
        valid_alts[0] if valid_alts else "",
    )


def get_channel_color_class(channel: str) -> str:
    if "1064" in channel:
        return "btn-ir"
    if "532" in channel:
        return "btn-vis"
    if "355" in channel:
        return "btn-uv"
    return "btn-default"


def make_channel_buttons(valid_channels: list[str], default_ch: str) -> str:
    buttons: list[str] = []

    for ch in valid_channels:
        color_class = get_channel_color_class(ch)
        active_class = " active" if ch == default_ch else ""
        label = html.escape(ch.replace("_", " "))

        buttons.append(
            f'<button class="tab-btn ch-btn {color_class}{active_class}" '
            f'onclick="setChannel({js_string(ch)}, this)">{label}</button>'
        )

    return "\n          ".join(buttons)


def make_altitude_buttons(valid_alts: list[str], default_alt: str) -> str:
    buttons: list[str] = []

    for alt in valid_alts:
        active_class = " active" if alt == default_alt else ""
        label = html.escape(f"{alt} km")

        buttons.append(
            f'<button class="tab-btn alt-btn{active_class}" '
            f'onclick="setAltitude({js_string(alt)}, this)">{label}</button>'
        )

    return "\n          ".join(buttons)


# =============================================================================
# HTML DASHBOARD GENERATOR
# =============================================================================

def generate_html_dashboard(
    html_path: str | Path,
    prefix: str,
    date_title: str,
    valid_channels: list[str],
    valid_alts: list[str],
    has_global_mean: bool,
    mean_rcs_file: str,
    year: str,
    cloud_public_url: str,
    dry_run: bool = False,
) -> None:
    """Generates the static HTML dashboard embedding cloud images."""

    html_path = Path(html_path)

    default_ch = select_default_channel(valid_channels)
    default_alt = select_default_altitude(valid_alts)

    channel_buttons = make_channel_buttons(valid_channels, default_ch)
    altitude_buttons = make_altitude_buttons(valid_alts, default_alt)

    global_tab_style = "display: inline-flex;" if has_global_mean else "display: none;"
    cloud_base_url = f"{cloud_public_url.rstrip('/')}/{year}"

    date_title_html = html.escape(date_title)
    initial_img_src = (
        f"{cloud_base_url}/Quicklook_{prefix}_{default_ch}_{default_alt}km.webp"
    )

    html_content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SPU Lidar | {date_title_html}</title>

  <style type="text/css">
    :root {{
        --topbar-bg: #1a1a1a;
        --page-bg: #f0f2f5;
        --panel-bg: #ffffff;
        --text-main: #333;
        --text-muted: #777;
        --brand-blue: #0056b3;
    }}

    * {{
        box-sizing: border-box;
    }}

    html, body {{
        background: var(--page-bg);
        color: var(--text-main);
        font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        margin: 0;
        padding: 0;
        min-height: 100vh;
        overflow-x: hidden;
        overflow-y: auto;
    }}

    body {{
        display: flex;
        flex-direction: column;
    }}

    .top-bar {{
        background: var(--topbar-bg);
        color: #fff;
        padding: 10px 25px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        flex-wrap: wrap;
        box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        min-height: 40px;
    }}

    .top-bar h2 {{
        margin: 0;
        font-size: 18px;
        font-weight: 500;
        letter-spacing: 1px;
        line-height: 1.25;
    }}

    .top-bar .date {{
        font-weight: 700;
        color: #4fc3f7;
        margin-left: 5px;
    }}

    .metadata {{
        font-size: 12px;
        color: #aaa;
        font-family: monospace;
        display: flex;
        gap: 15px;
        flex-wrap: wrap;
        white-space: nowrap;
    }}

    .toolbar {{
        background: var(--panel-bg);
        border-bottom: 1px solid #ddd;
        padding: 8px 25px;
        display: flex;
        justify-content: center;
        align-items: center;
        flex-wrap: wrap;
        gap: 24px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        min-height: 45px;
        position: sticky;
        top: 0;
        z-index: 10;
    }}

    .control-group {{
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        flex-wrap: wrap;
    }}

    .control-group h3 {{
        margin: 0;
        font-size: 11px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-right: 5px;
    }}

    .control-label-left {{
        margin-left: 15px;
        border-left: 2px solid #ddd;
        padding-left: 15px;
    }}

    .main-mode-btn {{
        background: transparent;
        border: none;
        font-size: 14px;
        font-weight: 600;
        color: var(--text-muted);
        cursor: pointer;
        padding: 6px 12px;
        border-bottom: 3px solid transparent;
        transition: 0.2s;
    }}

    .main-mode-btn:hover {{
        color: #111;
    }}

    .main-mode-btn.active {{
        color: var(--brand-blue);
        border-bottom: 3px solid var(--brand-blue);
    }}

    .tab-btn {{
        background: #f8f9fa;
        border: 1px solid #ccc;
        padding: 5px 12px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
        font-weight: 600;
        color: #555;
        transition: all 0.2s ease;
        font-family: monospace;
        min-height: 31px;
    }}

    .tab-btn:hover {{
        background: #e2e6ea;
        color: #111;
    }}

    .btn-ir.active {{
        background: #d32f2f;
        color: #fff;
        border-color: #b71c1c;
        box-shadow: 0 2px 4px rgba(211,47,47,0.3);
    }}

    .btn-vis.active {{
        background: #2e7d32;
        color: #fff;
        border-color: #1b5e20;
        box-shadow: 0 2px 4px rgba(46,125,50,0.3);
    }}

    .btn-uv.active {{
        background: #6a1b9a;
        color: #fff;
        border-color: #4a148c;
        box-shadow: 0 2px 4px rgba(106,27,154,0.3);
    }}

    .btn-default.active {{
        background: var(--brand-blue);
        color: #fff;
        border-color: #004085;
    }}

    .alt-btn.active {{
        background: #546e7a;
        color: #fff;
        border-color: #37474f;
    }}

    .image-container {{
        padding: 15px;
        text-align: center;
        min-height: calc(100vh - 145px);
        display: flex;
        justify-content: center;
        align-items: center;
        flex: 1;
    }}

    #main-display {{
        max-height: calc(100vh - 175px);
        max-width: 100%;
        object-fit: contain;
        box-shadow: 0 6px 16px rgba(0,0,0,0.15);
        background: #fff;
        cursor: zoom-in;
        transition: opacity 0.2s ease-in-out;
    }}

    .image-error {{
        display: none;
        max-width: 720px;
        background: #fff;
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 16px 20px;
        color: #555;
        font-size: 14px;
        line-height: 1.45;
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        text-align: left;
    }}

    .image-error.active {{
        display: block;
    }}

    #myModal {{
        display: none;
        position: fixed;
        z-index: 1000;
        inset: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0,0,0,0.9);
        backdrop-filter: blur(5px);
    }}

    .modal-close {{
        position: absolute;
        top: 15px;
        right: 30px;
        color: #bbb;
        font-size: 40px;
        font-weight: 300;
        cursor: pointer;
        line-height: 1;
    }}

    .modal-close:hover {{
        color: #fff;
    }}

    .modal-content {{
        margin: auto;
        display: block;
        max-width: 98%;
        max-height: 95vh;
        margin-top: 1%;
        animation: zoom 0.2s ease-out;
    }}

    @keyframes zoom {{
        from {{ transform: scale(0.95); opacity: 0; }}
        to {{ transform: scale(1); opacity: 1; }}
    }}

    @media (max-width: 900px) {{
        .top-bar {{
            justify-content: center;
            text-align: center;
            padding: 10px 16px;
        }}

        .toolbar {{
            gap: 14px;
            padding: 8px 14px;
        }}

        .control-label-left {{
            margin-left: 0;
            border-left: none;
            padding-left: 0;
        }}
    }}

    @media (max-width: 640px) {{
        .top-bar {{
            padding: 10px 12px;
            gap: 6px;
        }}

        .top-bar h2 {{
            width: 100%;
            font-size: 15px;
            letter-spacing: 0.5px;
        }}

        .metadata {{
            width: 100%;
            justify-content: center;
            gap: 8px 12px;
            font-size: 10px;
            white-space: normal;
        }}

        .toolbar {{
            position: static;
            padding: 10px 8px;
            gap: 10px;
        }}

        .control-group {{
            width: 100%;
            gap: 6px;
        }}

        .control-group h3 {{
            width: 100%;
            text-align: center;
            margin: 4px 0 2px 0;
            font-size: 10px;
        }}

        .main-mode-btn {{
            flex: 1 1 135px;
            font-size: 13px;
            padding: 8px 8px;
        }}

        .tab-btn {{
            flex: 1 1 88px;
            padding: 8px 8px;
            font-size: 11px;
            min-height: 38px;
        }}

        .image-container {{
            align-items: flex-start;
            padding: 10px 8px 18px 8px;
            min-height: auto;
        }}

        #main-display {{
            width: 100%;
            max-width: 100%;
            max-height: none;
            height: auto;
            box-shadow: 0 3px 10px rgba(0,0,0,0.18);
        }}

        .modal-close {{
            top: 10px;
            right: 18px;
            font-size: 34px;
        }}

        .modal-content {{
            max-width: 100%;
            max-height: 92vh;
            margin-top: 6vh;
        }}
    }}

    @media (max-width: 390px) {{
        .tab-btn {{
            flex-basis: 78px;
            font-size: 10px;
            padding-left: 6px;
            padding-right: 6px;
        }}

        .main-mode-btn {{
            font-size: 12px;
        }}
    }}
  </style>
</head>

<body>
  <div class="top-bar">
      <h2>SPU LIDAR STATION | <span class="date">{date_title_html}</span></h2>
      <div class="metadata">
          <span>LAT: 23.56°S</span>
          <span>LON: 46.73°W</span>
          <span>ELEV: 760m</span>
      </div>
  </div>

  <div class="toolbar">
      <div class="control-group">
          <button class="main-mode-btn active" id="tab-quicklooks" onclick="setMode('quicklooks')">RCS Maps</button>
          <button class="main-mode-btn" id="tab-resumo" style="{global_tab_style}" onclick="setMode('resumo')">Atmospheric Profiles</button>
      </div>

      <div class="control-group" id="controls-panel">
          <h3 class="control-label-left">Wavelength</h3>
          {channel_buttons}

          <h3 class="control-label-left">Range</h3>
          {altitude_buttons}
      </div>
  </div>

  <div class="image-container">
      <img
          id="main-display"
          src="{html.escape(initial_img_src)}"
          onclick="openModal(this.src)"
          alt="Lidar Data Image"
      >
      <div id="image-error" class="image-error"></div>
  </div>

  <div id="myModal">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <img class="modal-content" id="img01" alt="Expanded Lidar Data Image">
  </div>

  <script>
    var currentChannel = {js_string(default_ch)};
    var currentAltitude = {js_string(default_alt)};
    var prefix = {js_string(prefix)};
    var currentMode = "quicklooks";
    var cloudBaseUrl = {js_string(cloud_base_url)};
    var meanRcsFile = {js_string(mean_rcs_file)};

    var imgElement = document.getElementById("main-display");
    var imageError = document.getElementById("image-error");

    function buildImageUrl() {{
        if (currentMode === "quicklooks") {{
            return cloudBaseUrl + "/Quicklook_" + prefix + "_" + currentChannel + "_" + currentAltitude + "km.webp";
        }}
        return cloudBaseUrl + "/" + meanRcsFile;
    }}

    function updateImage() {{
        imageError.classList.remove("active");
        imageError.textContent = "";

        imgElement.style.display = "block";
        imgElement.style.opacity = 0.4;

        setTimeout(function() {{
            imgElement.src = buildImageUrl();
        }}, 100);
    }}

    imgElement.onload = function() {{
        imgElement.style.opacity = 1;
        imgElement.style.display = "block";
        imageError.classList.remove("active");
        imageError.textContent = "";
    }};

    imgElement.onerror = function() {{
        var failedUrl = imgElement.src;

        imgElement.style.opacity = 1;
        imgElement.style.display = "none";

        imageError.textContent =
            "Imagem não encontrada ou falha ao carregar. Verifique se o arquivo existe no R2/GitHub Pages: " +
            failedUrl;

        imageError.classList.add("active");
        console.error("Falha ao carregar:", failedUrl);
    }};

    function setMode(mode) {{
        currentMode = mode;

        document.getElementById("tab-quicklooks").classList.remove("active");
        document.getElementById("tab-resumo").classList.remove("active");

        if (mode === "quicklooks") {{
            document.getElementById("tab-quicklooks").classList.add("active");
            document.getElementById("controls-panel").style.display = "flex";
        }} else {{
            document.getElementById("tab-resumo").classList.add("active");
            document.getElementById("controls-panel").style.display = "none";
        }}

        updateImage();
    }}

    function setChannel(ch, btnElement) {{
        if (currentChannel === ch && currentMode === "quicklooks") return;

        currentChannel = ch;

        document.querySelectorAll(".ch-btn").forEach(function(btn) {{
            btn.classList.remove("active");
        }});

        btnElement.classList.add("active");
        updateImage();
    }}

    function setAltitude(alt, btnElement) {{
        if (currentAltitude === alt && currentMode === "quicklooks") return;

        currentAltitude = alt;

        document.querySelectorAll(".alt-btn").forEach(function(btn) {{
            btn.classList.remove("active");
        }});

        btnElement.classList.add("active");
        updateImage();
    }}

    var modal = document.getElementById("myModal");
    var modalImg = document.getElementById("img01");

    function openModal(src) {{
        if (!src || imgElement.style.display === "none") return;
        modal.style.display = "block";
        modalImg.src = src;
    }}

    function closeModal() {{
        modal.style.display = "none";
        modalImg.src = "";
    }}

    window.onclick = function(event) {{
        if (event.target === modal) closeModal();
    }};

    document.addEventListener("keydown", function(event) {{
        if (event.key === "Escape") closeModal();
    }});
  </script>
</body>
</html>
"""

    if dry_run:
        return

    html_path.parent.mkdir(parents=True, exist_ok=True)

    with open(html_path, "w", encoding="utf-8") as file:
        file.write(html_content)


# =============================================================================
# CALENDAR INTEGRATION
# =============================================================================

def update_calendar(
    base_site_folder: str | Path,
    logger: logging.Logger,
    dry_run: bool = False,
) -> None:
    """Scans the site folder and injects new entries into the JS calendar."""
    base_site_folder = Path(base_site_folder)
    calendar_file = "ql-measurement-calendar.html"
    calendar_path = base_site_folder / calendar_file
    default_color = "#A3E4D7"

    logger.info(f"Syncing interactive calendar ({calendar_file})...")

    if not calendar_path.exists():
        logger.warning(
            f"  -> [WARNING] Calendar file {calendar_file} not found in {base_site_folder}!"
        )
        return

    content = calendar_path.read_text(encoding="utf-8")

    existing_urls = set(re.findall(r"url:\s*['\"](.*?)['\"]", content))
    new_entries: list[str] = []

    for year_folder in sorted(base_site_folder.iterdir()):
        if not year_folder.is_dir():
            continue

        if not (year_folder.name.isdigit() and len(year_folder.name) == 4):
            continue

        for file_path in sorted(year_folder.iterdir()):
            if not (
                file_path.name.endswith("_Dashboard.html")
                or file_path.name.endswith("_Gallery.html")
            ):
                continue

            relative_url = f"{year_folder.name}/{file_path.name}"

            if relative_url in existing_urls:
                continue

            match = re.match(r"^(\d{4})(\d{2})(\d{2})", file_path.name)

            if not match:
                continue

            year = int(match.group(1))
            js_month = int(match.group(2)) - 1
            day = int(match.group(3))

            new_entries.append(
                "  {\n"
                f"    startDate: new Date({year}, {js_month}, {day}), "
                f"endDate: new Date({year}, {js_month}, {day}), "
                f"color: '{default_color}', "
                f"url: '{relative_url}'\n"
                "  },"
            )

    if not new_entries:
        logger.info("  -> Calendar is up to date. No new measurements found.")
        return

    marker = "// MARCADOR_AUTOMATICO"

    if marker not in content:
        logger.error("  -> [ERROR] Missing '// MARCADOR_AUTOMATICO' tag in your HTML.")
        return

    logger.info(f"  -> Inserting {len(new_entries)} new measurements into the calendar...")

    new_content = content.replace(
        marker,
        "\n".join(new_entries) + "\n  " + marker,
    )

    if dry_run:
        logger.info("  -> [DRY-RUN] Calendar would be updated.")
        return

    calendar_path.write_text(new_content, encoding="utf-8")
    logger.info("  -> [OK] Calendar successfully synced!")


# =============================================================================
# GITHUB PAGES AUTOMATION
# =============================================================================

def push_site_updates(
    site_dir: str | Path,
    logger: logging.Logger,
    no_push: bool = False,
    dry_run: bool = False,
) -> None:
    """Commits and pushes updates to the dedicated site repository."""
    if no_push:
        logger.info("Git push disabled by --no-push.")
        return

    if dry_run:
        logger.info("[DRY-RUN] Git add/commit/push would run now.")
        return

    try:
        import credentials

        gh_user = getattr(credentials, "GITHUB_USER", "spulidar")
        gh_token = credentials.GITHUB_TOKEN

    except (ImportError, AttributeError):
        logger.error("  -> [ERROR] GitHub token not found in credentials.py! Push aborted.")
        return

    logger.info("=== Pushing updates to measurements repository ===")

    original_work_dir = Path.cwd()
    site_dir = Path(site_dir)

    try:
        os.chdir(site_dir)

        subprocess.run(["git", "add", "."], check=True)

        commit_result = subprocess.run(
            ["git", "commit", "-m", "Auto-update HTML dashboards and calendar"],
            capture_output=True,
            text=True,
        )

        commit_output = commit_result.stdout + commit_result.stderr

        if commit_result.returncode != 0:
            if "nothing to commit" in commit_output.lower():
                logger.info("  -> Nothing to commit.")
                return

            logger.error("  -> [ERROR] Git commit failed.")
            logger.debug(commit_output)
            return

        auth_repo_url = f"https://{gh_token}@github.com/{gh_user}/measurements.git"

        push_result = subprocess.run(
            ["git", "push", auth_repo_url, "main"],
            capture_output=True,
            text=True,
        )

        if push_result.returncode == 0:
            logger.info("  -> [OK] Successfully pushed to measurements repository!")
        else:
            logger.error(
                "  -> [ERROR] Git push failed. Check your internet or token permissions."
            )
            logger.debug(push_result.stderr.replace(gh_token, "***HIDDEN_TOKEN***"))

    except subprocess.CalledProcessError:
        logger.error("  -> [CRITICAL] A Git command failed before pushing.")

    finally:
        os.chdir(original_work_dir)


# =============================================================================
# MEASUREMENT DISCOVERY
# =============================================================================

def collect_measurements(all_images: list[str], logger: logging.Logger) -> dict[str, MeasurementData]:
    measurements: dict[str, MeasurementData] = {}

    for img_path in all_images:
        img_name = os.path.basename(img_path)

        if img_name.startswith("Quicklook_"):
            parts = img_name.replace(".webp", "").split("_")

            if len(parts) < 5:
                logger.warning(f"Skipping malformed quicklook filename: {img_name}")
                continue

            prefix = parts[1]
            ch = f"{parts[2]}_{parts[3]}"
            alt = parts[4].replace("km", "")

            data = measurements.setdefault(prefix, MeasurementData())
            data.files.append(img_path)
            data.channels.add(ch)
            data.alts.add(alt)

        elif img_name.startswith("GlobalMeanRCS_"):
            parts = img_name.replace(".webp", "").split("_")

            if len(parts) < 2:
                logger.warning(f"Skipping malformed global mean filename: {img_name}")
                continue

            prefix = parts[1]

            data = measurements.setdefault(prefix, MeasurementData())
            data.files.append(img_path)
            data.has_global_mean = True
            data.mean_rcs_filename = img_name

    return measurements


def sort_altitudes(alts: set[str]) -> list[str]:
    def key_func(value: str) -> float:
        try:
            return float(value)
        except ValueError:
            return 0.0

    return sorted(alts, key=key_func)


# =============================================================================
# CLI / MAIN
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MILGRAU LIMP publisher: R2 upload + GitHub Pages HTML generation."
    )

    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml. Default: config.yaml",
    )

    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Rebuild HTML dashboards only. Disables Cloudflare uploads.",
    )

    parser.add_argument(
        "--sync-missing-uploads",
        action="store_true",
        help="Upload only images missing from Cloudflare R2.",
    )

    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Do not commit/push the measurements repository.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write files, upload images, or push changes.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = load_config(args.config)
    logger = setup_logger("update-site", config["directories"]["log_dir"])

    logger.info("=== Starting LIMP: Cloudflare R2 Upload & HTML Generation ===")

    root_dir = Path.cwd().parent
    base_data_folder = root_dir / config["directories"]["processed_data"]
    base_site_folder = root_dir / config.get("directories", {}).get(
        "site_output",
        "measurements",
    )

    ensure_directories(base_site_folder)

    incremental = bool(config["processing"].get("incremental", False))

    s3_client, bucket_name, cloud_public_url = get_cloud_credentials(logger)

    search_pattern = str(base_data_folder / "**" / "*.webp")
    all_images = sorted(glob.glob(search_pattern, recursive=True))

    if not all_images:
        logger.warning(f"No '.webp' images found in {base_data_folder}. Exiting.")
        return 0

    measurements = collect_measurements(all_images, logger)

    if not measurements:
        logger.warning("No valid measurement images found. Exiting.")
        return 0

    cloud_existing_keys: set[str] = set()

    if args.html_only:
        logger.info("HTML-ONLY MODE ACTIVE: Cloudflare uploads are disabled.")

    elif args.sync_missing_uploads:
        cloud_existing_keys = get_cloud_existing_keys(
            s3_client,
            bucket_name,
            logger,
        )

    processed_days = 0

    for prefix, data in sorted(measurements.items()):
        try:
            year = prefix[:4]
            dt = datetime.strptime(prefix[:8], "%Y%m%d")
            date_str = dt.strftime("%d %b %Y")
        except ValueError:
            year, date_str = "Unknown", prefix

        site_year_folder = base_site_folder / year
        html_path = site_year_folder / f"{prefix}_Dashboard.html"

        if (
            incremental
            and html_path.exists()
            and not args.html_only
            and not args.sync_missing_uploads
        ):
            logger.debug(f"  -> [SKIPPED] Dashboard already exists for: {prefix}")
            continue

        if not args.html_only:
            for img_path in sorted(data.files):
                filename = os.path.basename(img_path)
                cloud_path = f"{year}/{filename}"

                if args.sync_missing_uploads:
                    if cloud_path not in cloud_existing_keys:
                        logger.info(
                            f"      [SYNC] Missing on Cloudflare -> Uploading {filename}"
                        )

                        if not args.dry_run:
                            upload_to_r2(
                                s3_client,
                                bucket_name,
                                img_path,
                                cloud_path,
                                logger,
                            )

                        cloud_existing_keys.add(cloud_path)

                else:
                    logger.info(f"  -> [UPLOADING] Sending {filename}...")

                    if not args.dry_run:
                        upload_to_r2(
                            s3_client,
                            bucket_name,
                            img_path,
                            cloud_path,
                            logger,
                        )

        valid_channels = sorted(list(data.channels))
        valid_alts = sort_altitudes(data.alts)

        if not valid_channels:
            logger.warning(f"  -> [SKIPPED] No quicklook channels found for: {prefix}")
            continue

        logger.info(f"  -> Generating dashboard: {html_path}")

        generate_html_dashboard(
            html_path=html_path,
            prefix=prefix,
            date_title=date_str,
            valid_channels=valid_channels,
            valid_alts=valid_alts,
            has_global_mean=data.has_global_mean,
            mean_rcs_file=data.mean_rcs_filename,
            year=year,
            cloud_public_url=cloud_public_url,
            dry_run=args.dry_run,
        )

        processed_days += 1

    if args.html_only:
        logger.info(f"=== HTML REBUILD Finished! {processed_days} dashboards updated. ===")
    elif args.sync_missing_uploads:
        logger.info(
            f"=== CLOUD SYNC Finished! Missing files uploaded and "
            f"{processed_days} dashboards verified. ==="
        )
    else:
        logger.info(f"=== LIMP Finished! {processed_days} dashboards generated. ===")

    update_calendar(base_site_folder, logger, dry_run=args.dry_run)

    push_site_updates(
        base_site_folder,
        logger,
        no_push=args.no_push,
        dry_run=args.dry_run,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
