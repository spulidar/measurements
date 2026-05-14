"""
MILGRAU Suite - Web Publisher (LIMP)
Collects Level 1 graphics (.webp), uploads them to Cloudflare R2,
generates static HTML Dashboards per year pointing to the cloud CDN,
and automatically updates the interactive measurement calendar.
"""

import os
import sys
import glob
import re
import boto3
from datetime import datetime
from pathlib import Path

def load_config(config_path: str = "config.yaml") -> dict:
    """
    Loads and minimally validates the MILGRAU YAML configuration file.

    Parameters
    ----------
    config_path : str
        Path to the YAML file. Defaults to ``config.yaml`` in the current
        working directory.

    Returns
    -------
    dict
        Parsed configuration dictionary.
    """
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
    """
    Creates a standardized logger for MILGRAU modules.

    The function clears pre-existing handlers from the same logger name to avoid
    duplicated messages in notebooks, IDE sessions, or repeated script calls.
    """
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
    """
    Creates one or more directories if they do not already exist.

    Parameters
    ----------
    *directories
        Directory paths to create.
    """
    for directory in directories:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logging.error(f"Failed to create directory {directory}: {exc}")

# ==========================================
# CLOUDFLARE R2 SETUP & CREDENTIALS
# ==========================================
def get_cloud_credentials(logger):
    """Safely loads R2 credentials and initializes the boto3 client."""
    try:
        import credentials
        s3_client = boto3.client('s3',
            endpoint_url=credentials.R2_ENDPOINT,
            aws_access_key_id=credentials.R2_ACCESS_KEY,
            aws_secret_access_key=credentials.R2_SECRET_KEY,
            region_name='auto' 
        )
        return s3_client, credentials.R2_BUCKET_NAME, credentials.R2_PUBLIC_URL
    except ImportError:
        logger.critical("'credentials.py' not found! Please create it with your R2 keys. Exiting.")
        sys.exit(1)
    except AttributeError as e:
        logger.critical(f"Missing required variable in credentials.py: {e}. Exiting.")
        sys.exit(1)

def upload_to_r2(s3_client, bucket_name, local_file_path, cloud_file_key, logger):
    """Uploads a single file to the Cloudflare R2 Bucket."""
    try:
        s3_client.upload_file(local_file_path, bucket_name, cloud_file_key)
        return True
    except Exception as e:
        logger.error(f"  -> [R2 UPLOAD ERROR] Failed to upload {local_file_path}: {e}")
        return False

# ==========================================
# HTML DASHBOARD GENERATOR
# ==========================================
def generate_html_dashboard(html_path, prefix, date_title, valid_channels, valid_alts, has_global_mean, mean_rcs_file, year, cloud_public_url):
    """Generates the static HTML dashboard embedding cloud images (Compact Light Theme)."""
    
    # --- SMART DEFAULT SELECTION (Case-Insensitive Fix) ---
    # Convert 'ch' to lower() so "532nm_AN" matches "an" successfully
    default_ch = next((ch for ch in valid_channels if "532" in ch and "an" in ch.lower()), valid_channels[0] if valid_channels else "")
    default_alt = next((alt for alt in valid_alts if "15" == alt or "15.0" == alt), valid_alts[0] if valid_alts else "")
    
    # Physics colored buttons adapted for Light Theme + Sync Active State
    channel_buttons = ""
    for ch in valid_channels:
        color_class = "btn-default"
        if "1064" in ch: color_class = "btn-ir"
        elif "532" in ch: color_class = "btn-vis"
        elif "355" in ch: color_class = "btn-uv"
        
        # Add the 'active' CSS class exactly to the default channel
        active_class = " active" if ch == default_ch else ""
        channel_buttons += f'<button class="tab-btn ch-btn {color_class}{active_class}" onclick="setChannel(\'{ch}\', this)">{ch.replace("_", " ")}</button>\n'
        
    altitude_buttons = ""
    for alt in valid_alts:
        # Add the 'active' CSS class exactly to the default altitude
        active_class = " active" if alt == default_alt else ""
        altitude_buttons += f'<button class="tab-btn alt-btn{active_class}" onclick="setAltitude(\'{alt}\', this)">{alt} km</button>\n'

    global_tab_style = "display: flex;" if has_global_mean else "display: none;"
    cloud_base_url = f"{cloud_public_url}/{year}"

    html_content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SPU Lidar | {date_title}</title>
  <style type="text/css">
    /* Compact Light Theme matching Google Sites */
    html, body {{ 
        background: #f0f2f5; 
        color: #333; 
        font-family: 'Segoe UI', Roboto, Helvetica, sans-serif; 
        margin: 0; 
        padding: 0; 
        height: 100vh;
        overflow: hidden; 
    }}
    
    /* Top Menu Bar */
    .top-bar {{ 
        background: #1a1a1a; 
        color: #fff; 
        padding: 10px 25px; 
        display: flex; 
        justify-content: space-between; 
        align-items: center; 
        box-shadow: 0 2px 4px rgba(0,0,0,0.2); 
        height: 40px;
    }}
    .top-bar h2 {{ margin: 0; font-size: 18px; font-weight: 500; letter-spacing: 1px; }}
    .top-bar .date {{ font-weight: 700; color: #4fc3f7; margin-left: 5px; }}
    .metadata {{ font-size: 12px; color: #aaa; font-family: monospace; display: flex; gap: 15px; }}
    
    /* Toolbar (Mode + Controls) */
    .toolbar {{ 
        background: #ffffff; 
        border-bottom: 1px solid #ddd; 
        padding: 8px 25px; 
        display: flex; 
        justify-content: center; 
        align-items: center; 
        flex-wrap: wrap; 
        gap: 30px; 
        box-shadow: 0 2px 8px rgba(0,0,0,0.05); 
        height: 45px;
    }}
    
    .control-group {{ display: flex; align-items: center; gap: 8px; }}
    .control-group h3 {{ margin: 0; font-size: 11px; color: #777; text-transform: uppercase; letter-spacing: 1px; margin-right: 5px; }}
    
    /* Toolbar Buttons */
    .main-mode-btn {{ background: transparent; border: none; font-size: 14px; font-weight: 600; color: #777; cursor: pointer; padding: 6px 12px; border-bottom: 3px solid transparent; transition: 0.2s; }}
    .main-mode-btn:hover {{ color: #111; }}
    .main-mode-btn.active {{ color: #0056b3; border-bottom: 3px solid #0056b3; }}
    
    .tab-btn {{ background: #f8f9fa; border: 1px solid #ccc; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; color: #555; transition: all 0.2s ease; font-family: monospace; }}
    .tab-btn:hover {{ background: #e2e6ea; color: #111; }}
    
    /* Active Physics Colors */
    .btn-ir.active {{ background: #d32f2f; color: #fff; border-color: #b71c1c; box-shadow: 0 2px 4px rgba(211,47,47,0.3); }}
    .btn-vis.active {{ background: #2e7d32; color: #fff; border-color: #1b5e20; box-shadow: 0 2px 4px rgba(46,125,50,0.3); }}
    .btn-uv.active {{ background: #6a1b9a; color: #fff; border-color: #4a148c; box-shadow: 0 2px 4px rgba(106,27,154,0.3); }}
    .btn-default.active {{ background: #0056b3; color: #fff; border-color: #004085; }}
    .alt-btn.active {{ background: #546e7a; color: #fff; border-color: #37474f; }}

    /* Image Display Area */
    .image-container {{ 
        padding: 15px; 
        text-align: center; 
        height: calc(100vh - 145px); 
        display: flex; 
        justify-content: center; 
        align-items: center; 
    }}
    
    #main-display {{ 
        max-height: 100%; 
        max-width: 100%; 
        object-fit: contain; 
        box-shadow: 0 6px 16px rgba(0,0,0,0.15); 
        background: #fff; 
        cursor: zoom-in; 
        transition: opacity 0.2s ease-in-out; 
    }}
    
    /* Fullscreen Modal */
    #myModal {{ display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.9); backdrop-filter: blur(5px); }}
    .modal-close {{ position: absolute; top: 15px; right: 30px; color: #bbb; font-size: 40px; font-weight: 300; cursor: pointer; }}
    .modal-close:hover {{ color: #fff; }}
    .modal-content {{ margin: auto; display: block; max-width: 98%; max-height: 95vh; margin-top: 1%; animation: zoom 0.2s ease-out; }}
    @keyframes zoom {{ from {{transform:scale(0.95); opacity:0}} to {{transform:scale(1); opacity:1}} }}
  </style>
</head>
<body>

  <div class="top-bar">
      <h2>SPU LIDAR STATION | <span class="date">{date_title}</span></h2>
      <div class="metadata">
          <span>LAT: 23.56°S</span>
          <span>LON: 46.73°W</span>
          <span>ELEV: 760m</span
      </div>
  </div>
  
  <div class="toolbar">
      <div class="control-group">
          <button class="main-mode-btn active" id="tab-quicklooks" onclick="setMode('quicklooks')">RCS Maps</button>
          <button class="main-mode-btn" id="tab-resumo" style="{global_tab_style}" onclick="setMode('resumo')">Atmospheric Profiles</button>
      </div>
      
      <div class="control-group" id="controls-panel">
          <h3 style="margin-left: 15px; border-left: 2px solid #ddd; padding-left: 15px;">Wavelength</h3>
          {channel_buttons}
          <h3 style="margin-left: 10px;">Range</h3>
          {altitude_buttons}
      </div>
  </div>
  
  <div class="image-container">
      <img id="main-display" src="{cloud_base_url}/Quicklook_{prefix}_{default_ch}_{default_alt}km.webp" onclick="openModal(this.src)" alt="Lidar Data Image">
  </div>
  
  <div id="myModal">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <img class="modal-content" id="img01">
  </div>

  <script>
    var currentChannel = "{default_ch}";
    var currentAltitude = "{default_alt}";
    var prefix = "{prefix}";
    var currentMode = "quicklooks";
    var cloudBaseUrl = "{cloud_base_url}";
    var imgElement = document.getElementById("main-display");

    // REMOVIDO: O Javascript burro que selecionava os primeiros botões cegamente
    // document.addEventListener("DOMContentLoaded", function() {{ ... }});

    function updateImage() {{
        imgElement.style.opacity = 0.4;
        setTimeout(() => {{
            if (currentMode === "quicklooks") {{
                imgElement.src = cloudBaseUrl + "/Quicklook_" + prefix + "_" + currentChannel + "_" + currentAltitude + "km.webp";
            }} else {{
                imgElement.src = cloudBaseUrl + "/{mean_rcs_file}";
            }}
        }}, 100);
    }}

    imgElement.onload = function() {{ imgElement.style.opacity = 1; }};

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
        if(currentChannel === ch && currentMode === "quicklooks") return; 
        currentChannel = ch;
        document.querySelectorAll(".ch-btn").forEach(btn => btn.classList.remove("active"));
        btnElement.classList.add("active");
        updateImage();
    }}

    function setAltitude(alt, btnElement) {{
        if(currentAltitude === alt && currentMode === "quicklooks") return;
        currentAltitude = alt;
        document.querySelectorAll(".alt-btn").forEach(btn => btn.classList.remove("active"));
        btnElement.classList.add("active");
        updateImage();
    }} 

    var modal = document.getElementById("myModal");
    var modalImg = document.getElementById("img01");
    
    function openModal(src) {{ modal.style.display = "block"; modalImg.src = src; }}
    function closeModal() {{ modal.style.display = "none"; }}
    window.onclick = function(event) {{ if (event.target == modal) closeModal(); }}
    document.addEventListener('keydown', function(event) {{ if (event.key === "Escape") closeModal(); }});
  </script>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

# ==========================================
# CALENDAR INTEGRATION
# ==========================================
def update_calendar(base_site_folder, logger):
    """Scans the site folder and injects new entries into the JS calendar."""
    calendar_file = "ql-measurement-calendar.html"
    logger.info(f"Syncing interactive calendar ({calendar_file})...")
    
    calendar_path = os.path.join(base_site_folder, calendar_file)
    default_color = '#A3E4D7'
    
    if not os.path.exists(calendar_path):
        logger.warning(f"  -> [WARNING] Calendar file {calendar_file} not found in {base_site_folder}!")
        return

    with open(calendar_path, 'r', encoding='utf-8') as f:
        content = f.read()

    existing_urls = set(re.findall(r"url:\s*'(.*?)'", content))
    new_entries = ""
    counter = 0

    for year_folder in os.listdir(base_site_folder):
        year_path = os.path.join(base_site_folder, year_folder)
        
        if os.path.isdir(year_path) and year_folder.isdigit() and len(year_folder) == 4:
            for file in os.listdir(year_path):
                if file.endswith('_Dashboard.html') or file.endswith('_Gallery.html'):
                    relative_url = f"{year_folder}/{file}"
                    
                    if relative_url not in existing_urls:
                        match = re.match(r'^(\d{4})(\d{2})(\d{2})', file)
                        if match:
                            year = int(match.group(1))
                            js_month = int(match.group(2)) - 1 
                            day = int(match.group(3))
                            
                            new_entries += f"  {{\n    startDate: new Date({year}, {js_month}, {day}), endDate: new Date({year}, {js_month}, {day}), color: '{default_color}', url: '{relative_url}'\n  }},\n"
                            counter += 1

    if counter == 0:
        logger.info("  -> Calendar is up to date. No new measurements found.")
    else:
        logger.info(f"  -> Inserting {counter} new measurements into the calendar...")
        if "// MARCADOR_AUTOMATICO" in content:
            new_content = content.replace("// MARCADOR_AUTOMATICO", new_entries + "  // MARCADOR_AUTOMATICO")
            with open(calendar_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            logger.info("  -> [OK] Calendar successfully synced!")
        else:
            logger.error("  -> [ERROR] Missing '// MARCADOR_AUTOMATICO' tag in your HTML.")


# ==========================================
# GITHUB PAGES AUTOMATION
# ==========================================
def push_site_updates(site_dir, logger):
    """Commits and pushes updates to the dedicated site repository using a secure token."""
    import subprocess
    import sys
    
    # Safely load GitHub credentials
    try:
        import credentials
        gh_user = getattr(credentials, 'GITHUB_USER', 'spulidar')
        gh_token = credentials.GITHUB_TOKEN
    except (ImportError, AttributeError):
        logger.error("  -> [ERROR] GitHub token not found in credentials.py! Push aborted.")
        return

    logger.info("=== Pushing updates to measurements repository ===")
    
    original_work_dir = os.getcwd()
    os.chdir(site_dir)
    
    try:
        subprocess.run(["git", "add", "."], check=True)
        # Using check=False because it might not have new files to commit
        subprocess.run(["git", "commit", "-m", "Auto-update HTML dashboards and calendar"], check=False)
        
        # Build the authenticated URL
        # Format: https://<token>@github.com/<user>/<repo>.git
        auth_repo_url = f"https://{gh_token}@github.com/{gh_user}/measurements.git"
        
        # Execute push with the secure URL
        # We catch the error manually to prevent the token from being printed in the terminal logs
        result = subprocess.run(
            ["git", "push", auth_repo_url, "main"], 
            capture_output=True, 
            text=True
        )
        
        if result.returncode == 0:
            logger.info("  -> [OK] Successfully pushed to measurements repository!")
        else:
            logger.error("  -> [ERROR] Git push failed. (Check your internet or token permissions)")
            logger.debug(result.stderr.replace(gh_token, "***HIDDEN_TOKEN***")) # Hides token in debug logs
            
    except subprocess.CalledProcessError as err:
        logger.error("  -> [CRITICAL] A Git command failed before pushing.")
    finally:
        os.chdir(original_work_dir)

# ==========================================
# MAIN ROUTINE (THE "VACUUM")
# ==========================================
if __name__ == "__main__":
    config = load_config()
    logger = setup_logger("update-site", config['directories']['log_dir'])
    logger.info("=== Starting LIMP (Cloudflare R2 Upload & HTML Generation) ===")

    REBUILD_HTML_ONLY = False  # True: recria HTMLs e desliga uploads.
    SYNC_MISSING_UPLOADS = True # True: vasculha a nuvem e faz upload APENAS do que faltou.

    # Setup directories
    root_dir = Path.cwd().parent
    base_data_folder = os.path.join(root_dir, config['directories']['processed_data'])
    base_site_folder = os.path.join(root_dir, config.get('directories', {}).get('site_output', 'measurements'))
    ensure_directories(base_site_folder)
    
    incremental = config['processing']['incremental']
    s3_client, bucket_name, cloud_public_url = get_cloud_credentials(logger)

    # 1. Vacuum Graphic Files
    search_pattern = os.path.join(base_data_folder, '**', '*.webp')
    all_images = glob.glob(search_pattern, recursive=True)
    
    if not all_images:
        logger.warning(f"No '.webp' images found in {base_data_folder}. Exiting.")
        sys.exit(0)

    # 2. Aggregate Data by Measurement Date/Period
    measurements = {}
    for img_path in all_images:
        img_name = os.path.basename(img_path)
        
        if img_name.startswith("Quicklook_"):
            parts = img_name.replace(".webp", "").split("_")
            if len(parts) >= 5:
                prefix = parts[1]
                ch = f"{parts[2]}_{parts[3]}" 
                alt = parts[4].replace("km", "")
                
                if prefix not in measurements:
                    measurements[prefix] = {'files': [], 'channels': set(), 'alts': set(), 'has_global_mean': False, 'mean_rcs_filename': ""}
                
                measurements[prefix]['files'].append(img_path)
                measurements[prefix]['channels'].add(ch)
                measurements[prefix]['alts'].add(alt)
                
        elif img_name.startswith("GlobalMeanRCS_"):
            parts = img_name.replace(".webp", "").split("_")
            if len(parts) >= 2:
                prefix = parts[1]
                
                if prefix not in measurements:
                    measurements[prefix] = {'files': [], 'channels': set(), 'alts': set(), 'has_global_mean': False, 'mean_rcs_filename': ""}
                
                measurements[prefix]['files'].append(img_path)
                measurements[prefix]['has_global_mean'] = True
                measurements[prefix]['mean_rcs_filename'] = img_name

    # 3. Process Uploads and HTML
    processed_days = 0
    
    # --- SMART SYNC CLOUD CACHE ---
    cloud_existing_keys = set()
    
    if REBUILD_HTML_ONLY:
        logger.info("REBUILD MODE ACTIVE: Overwriting HTMLs. Cloudflare uploads are disabled.")
    elif SYNC_MISSING_UPLOADS:
        logger.info("SYNC MODE ACTIVE: Fetching cloud index (this takes a few seconds)...")
        paginator = s3_client.get_paginator('list_objects_v2')
        try:
            for page in paginator.paginate(Bucket=bucket_name):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        cloud_existing_keys.add(obj['Key'])
            logger.info(f"Cloud index built: {len(cloud_existing_keys)} files currently on Cloudflare.")
        except Exception as e:
            logger.error(f"Failed to fetch cloud index: {e}")
            sys.exit(1)
        
    for prefix, data in measurements.items():
        try:
            year = prefix[:4]
            dt = datetime.strptime(prefix[:8], "%Y%m%d")
            date_str = dt.strftime("%d %b %Y")
        except ValueError:
            year, date_str = "Unknown", prefix

        site_year_folder = os.path.join(base_site_folder, year)
        ensure_directories(site_year_folder)
        html_path = os.path.join(site_year_folder, f"{prefix}_Dashboard.html")

        if incremental and os.path.exists(html_path) and not REBUILD_HTML_ONLY and not SYNC_MISSING_UPLOADS:
            logger.debug(f"  -> [SKIPPED] Dashboard already exists for: {prefix}")
            continue
            
        # Upload Control
        if not REBUILD_HTML_ONLY:
            for img_path in data['files']:
                filename = os.path.basename(img_path)
                cloud_path = f"{year}/{filename}"
                
                if SYNC_MISSING_UPLOADS:
                    # ULTRA-FAST MEMORY CHECK
                    if cloud_path not in cloud_existing_keys:
                        logger.info(f"      [SYNC] Missing on Cloudflare -> Uploading {filename}")
                        upload_to_r2(s3_client, bucket_name, img_path, cloud_path, logger)
                        cloud_existing_keys.add(cloud_path) # Add to memory to avoid duplicate attempts
                else:
                    # Original behavior
                    logger.info(f"  -> [UPLOADING] Sending {filename}...")
                    upload_to_r2(s3_client, bucket_name, img_path, cloud_path, logger)
            
        # HTML Generation
        valid_channels = sorted(list(data['channels']))
        valid_alts = sorted(list(data['alts']), key=lambda x: float(x) if x.replace('.','',1).isdigit() else 0)
        
        if valid_channels:
            generate_html_dashboard(
                html_path, prefix, date_str, valid_channels, valid_alts, 
                data['has_global_mean'], data['mean_rcs_filename'], year, cloud_public_url
            )
            processed_days += 1
    if REBUILD_HTML_ONLY:
        logger.info(f"=== HTML REBUILD Finished! {processed_days} dashboards updated. ===")
    elif SYNC_MISSING_UPLOADS:
        logger.info(f"=== CLOUD SYNC Finished! Missing files uploaded and {processed_days} dashboards verified. ===")
    else:
        logger.info(f"=== LIMP Finished! {processed_days} new dashboards generated. ===")
    
    # 4. Sync Calendar
    update_calendar(base_site_folder, logger)
    push_site_updates(base_site_folder, logger)
