"""Existing measurements daily dashboard visual renderer fed only by published state-v2."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import html
import json
from pathlib import Path
import re

from .windows import PUBLICATION_WINDOW_LABELS, PUBLICATION_WINDOW_STARTS

_FINGERPRINT_RE = re.compile(r'<meta name="publisher-fingerprint" content="([0-9a-f]+)">')


def day_output_path(site_root: str | Path, date: str) -> Path:
    root = Path(site_root)
    return root / date[:4] / date[4:6] / date / "index.html"


def read_dashboard_fingerprint(path: str | Path) -> str | None:
    target = Path(path)
    if not target.exists():
        return None
    match = _FINGERPRINT_RE.search(target.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else None


def _renderer_fingerprint() -> str:
    digest = sha256()
    for path in (Path(__file__), Path(__file__).with_name("site_v2.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def dashboard_fingerprint(date: str, day: dict, public_url: str = "") -> str:
    digest = sha256()
    digest.update(_renderer_fingerprint().encode("ascii"))
    digest.update(date.encode("ascii"))
    digest.update(public_url.rstrip("/").encode("utf-8"))
    digest.update(
        json.dumps(day, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return digest.hexdigest()


def _public_url(base: str, key: str | None) -> str | None:
    if not key:
        return None
    return f"{base.rstrip('/')}/{key.lstrip('/')}"


def _payload(date: str, day: dict, public_url: str) -> dict:
    """Adapt verified schema-v2 windows to the unchanged day-page JavaScript contract."""
    views: list[dict] = []
    windows = day.get("publication_windows", {})
    for hour in PUBLICATION_WINDOW_STARTS:
        if hour not in windows:
            continue
        record = windows[hour]
        pub_id = record["publication_window_id"]
        channels: dict[str, dict[str, str]] = {}
        for filename, asset in record["assets"].items():
            prefix = f"rcs_{pub_id}_"
            if not filename.startswith(prefix) or not filename.endswith(".webp"):
                raise ValueError(f"Unexpected asset for {pub_id}: {filename}")
            rest = filename[len(prefix):-5]
            channel, altitude = rest.rsplit("_", 1)
            if not altitude.endswith("km"):
                raise ValueError(f"Unexpected altitude in published asset: {filename}")
            channels.setdefault(channel, {})[altitude[:-2]] = (
                _public_url(public_url, asset["key"]) + "?v=" + asset["sha256"]
            )
        views.append(
            {
                "selector_id": hour,
                "publication_window_id": pub_id,
                "label": PUBLICATION_WINDOW_LABELS[hour],
                "channels": channels,
            }
        )
    return {"date": date, "publication_views": views}


def render_day_dashboard(
    date: str,
    day: dict,
    site_root: str | Path,
    public_url: str,
    dry_run: bool = False,
) -> Path:
    output_path = day_output_path(site_root, date)
    fingerprint = dashboard_fingerprint(date, day, public_url)
    payload_json = json.dumps(_payload(date, day, public_url), ensure_ascii=False).replace("</", "<\\/")
    date_title = datetime.strptime(date, "%Y%m%d").strftime("%d %b %Y")

    page = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="publisher-fingerprint" content="{fingerprint}">
  <title>SPU Lidar | {html.escape(date_title)}</title>
  <style>
    :root {{ --top:#1a1a1a; --page:#f0f2f5; --panel:#fff; --text:#333; --muted:#777; --blue:#0056b3; }}
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; min-height:100%; background:var(--page); color:var(--text); font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }}
    body {{ min-height:100vh; display:flex; flex-direction:column; }}
    .top-bar {{ background:var(--top); color:white; padding:10px 24px; display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap; box-shadow:0 2px 4px rgba(0,0,0,.2); }}
    .top-bar h2 {{ margin:0; font-size:18px; font-weight:500; letter-spacing:1px; }}
    .date {{ color:#4fc3f7; font-weight:700; }}
    .metadata {{ color:#aaa; font:12px monospace; display:flex; gap:15px; flex-wrap:wrap; }}
    .toolbar {{ position:sticky; top:0; z-index:10; background:var(--panel); border-bottom:1px solid #ddd; padding:8px 20px; display:flex; justify-content:center; align-items:center; flex-wrap:wrap; gap:18px; box-shadow:0 2px 8px rgba(0,0,0,.05); }}
    .control-group {{ display:flex; align-items:center; justify-content:center; gap:7px; flex-wrap:wrap; }}
    .control-group h3 {{ margin:0 4px 0 0; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:1px; }}
    .main-mode-btn {{ border:0; border-bottom:3px solid transparent; background:transparent; padding:7px 10px; color:var(--muted); font-weight:650; cursor:pointer; }}
    .main-mode-btn.active {{ color:var(--blue); border-bottom-color:var(--blue); }}
    .tab-btn {{ border:1px solid #ccc; border-radius:4px; background:#f8f9fa; color:#555; padding:6px 10px; min-height:31px; font:600 12px monospace; cursor:pointer; }}
    .tab-btn:hover {{ background:#e2e6ea; color:#111; }}
    .tab-btn.active {{ background:#546e7a; border-color:#37474f; color:#fff; }}
    .period-btn.active {{ background:var(--blue); border-color:#004085; }}
    .ch-btn.active {{ background:#2e7d32; border-color:#1b5e20; }}
    .image-container {{ padding:15px; text-align:center; min-height:calc(100vh - 150px); flex:1; display:flex; justify-content:center; align-items:center; }}
    #main-display {{ max-width:100%; max-height:calc(100vh - 180px); object-fit:contain; background:#fff; box-shadow:0 6px 16px rgba(0,0,0,.15); cursor:zoom-in; }}
    .message {{ display:none; max-width:720px; background:#fff; border:1px solid #ddd; border-radius:8px; padding:20px; line-height:1.5; box-shadow:0 4px 12px rgba(0,0,0,.08); }}
    .message.active {{ display:block; }}
    #myModal {{ display:none; position:fixed; z-index:1000; inset:0; background:rgba(0,0,0,.9); }}
    .modal-close {{ position:absolute; top:15px; right:30px; color:#bbb; font-size:40px; cursor:pointer; }}
    .modal-content {{ display:block; margin:2vh auto 0; max-width:98%; max-height:95vh; }}
    @media (max-width:700px) {{
      .top-bar {{ justify-content:center; text-align:center; padding:10px 12px; }}
      .metadata {{ width:100%; justify-content:center; font-size:10px; }}
      .toolbar {{ position:static; padding:10px 8px; gap:10px; }}
      .control-group {{ width:100%; }}
      .control-group h3 {{ width:100%; text-align:center; margin:3px 0; }}
      .tab-btn {{ flex:1 1 74px; min-height:38px; }}
      .image-container {{ align-items:flex-start; padding:10px 8px 18px; min-height:auto; }}
      #main-display {{ width:100%; max-height:none; height:auto; }}
    }}
  </style>
</head>
<body>
  <div class="top-bar">
    <h2>SPU LIDAR STATION | <span class="date">{html.escape(date_title)}</span></h2>
    <div class="metadata"><span>LAT: 23.56°S</span><span>LON: 46.73°W</span><span>ELEV: 760m</span></div>
  </div>

  <div class="toolbar">
    <div class="control-group" id="period-controls"><h3>Publication window</h3></div>
    <div class="control-group">
      <button class="main-mode-btn active" id="tab-quicklooks">RCS Maps</button>
    </div>
    <div class="control-group" id="channel-controls"><h3>Wavelength</h3></div>
    <div class="control-group" id="altitude-controls"><h3>Range</h3></div>
  </div>

  <div class="image-container">
    <img id="main-display" alt="Lidar publication view" onclick="openModal(this.src)">
    <div id="message" class="message"></div>
  </div>

  <div id="myModal"><span class="modal-close" onclick="closeModal()">&times;</span><img class="modal-content" id="img01" alt="Expanded lidar image"></div>

<script>
const dayData = {payload_json};
let currentSelectorId = dayData.publication_views.length ? dayData.publication_views[0].selector_id : '';
let currentChannel = '';
let currentAltitude = '';

const img = document.getElementById('main-display');
const message = document.getElementById('message');
const channelControls = document.getElementById('channel-controls');
const altitudeControls = document.getElementById('altitude-controls');

function clearButtons(container) {{ container.querySelectorAll('button').forEach(button => button.remove()); }}
function currentView() {{ return dayData.publication_views.find(view => view.selector_id === currentSelectorId) || null; }}
function showMessage(text) {{ img.style.display='none'; message.textContent=text; message.classList.add('active'); }}
function hideMessage() {{ message.classList.remove('active'); message.textContent=''; img.style.display='block'; }}

function renderWindows() {{
  const container=document.getElementById('period-controls');
  clearButtons(container);
  dayData.publication_views.forEach(view => {{
    const btn=document.createElement('button');
    btn.className='tab-btn period-btn'+(view.selector_id===currentSelectorId?' active':'');
    btn.textContent=view.label;
    if (view.publication_window_id) btn.title=view.publication_window_id;
    btn.onclick=()=>{{ currentSelectorId=view.selector_id; currentChannel=''; currentAltitude=''; renderAll(); }};
    container.appendChild(btn);
  }});
}}

function renderSelectors() {{
  clearButtons(channelControls); clearButtons(altitudeControls);
  const data=currentView();
  if (!data) {{ channelControls.style.display='none'; altitudeControls.style.display='none'; return; }}

  const channels=Object.keys(data.channels || {{}});
  if (!channels.length) {{ channelControls.style.display='none'; altitudeControls.style.display='none'; return; }}
  channelControls.style.display='flex'; altitudeControls.style.display='flex';

  if (!channels.includes(currentChannel)) currentChannel=channels.find(channel=>channel.includes('532nm_AN')) || channels[0];
  channels.forEach(channel => {{
    const btn=document.createElement('button');
    btn.className='tab-btn ch-btn'+(channel===currentChannel?' active':'');
    btn.textContent=channel.replace('_',' ');
    btn.onclick=()=>{{ currentChannel=channel; currentAltitude=''; renderAll(); }};
    channelControls.appendChild(btn);
  }});

  const altitudes=Object.keys((data.channels || {{}})[currentChannel] || {{}}).sort((a,b)=>parseFloat(a)-parseFloat(b));
  if (!altitudes.includes(currentAltitude)) currentAltitude=altitudes.includes('15') ? '15' : (altitudes[0] || '');
  altitudes.forEach(altitude => {{
    const btn=document.createElement('button');
    btn.className='tab-btn'+(altitude===currentAltitude?' active':'');
    btn.textContent=altitude+' km';
    btn.onclick=()=>{{ currentAltitude=altitude; renderAll(); }};
    altitudeControls.appendChild(btn);
  }});
}}

function updateDisplay() {{
  const data=currentView(); hideMessage();
  if (!data) {{ showMessage('No data are available for this day.'); return; }}

  let url='';
  url=(((data.channels || {{}})[currentChannel] || {{}})[currentAltitude]) || '';

  if (!url) {{ showMessage('No image is available for this view.'); return; }}
  img.style.opacity='.4'; img.src=url;
}}

function renderAll() {{ renderWindows(); renderSelectors(); updateDisplay(); }}

img.onload=()=>{{ img.style.opacity='1'; img.style.display='block'; message.classList.remove('active'); }};
img.onerror=()=>{{ img.style.opacity='1'; showMessage('Image not found or failed to load.'); }};
const modal=document.getElementById('myModal'), modalImg=document.getElementById('img01');
function openModal(src) {{ if (!src || img.style.display==='none') return; modal.style.display='block'; modalImg.src=src; }}
function closeModal() {{ modal.style.display='none'; modalImg.src=''; }}
window.onclick=event=>{{ if(event.target===modal) closeModal(); }};
document.addEventListener('keydown',event=>{{ if(event.key==='Escape') closeModal(); }});
renderAll();
</script>
</body>
</html>
'''

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(page, encoding="utf-8")
    return output_path
