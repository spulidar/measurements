#!/usr/bin/env python3
"""
Hotfix existing SPU Lidar dashboard HTML files.

Use this when you do NOT have the local .webp files available and only want to
patch already-generated GitHub Pages HTML dashboards.

What this script does:
- Fixes malformed '</span' tags.
- Replaces rigid desktop-only CSS with safer responsive CSS.
- Adds viewport meta tag for mobile.
- Adds a visible image loading error message.
- Patches JS image onload/onerror behavior.
- Does NOT read processed_data.
- Does NOT upload anything to Cloudflare R2.
- Does NOT regenerate dashboards from images.

Run from the measurements repository root:

    python scripts/hotfix_existing_dashboards.py --dry-run
    python scripts/hotfix_existing_dashboards.py --apply
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".github",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
}


RESPONSIVE_CSS = r"""
    :root {
        --topbar-bg: #1a1a1a;
        --page-bg: #f0f2f5;
        --panel-bg: #ffffff;
        --text-main: #333;
        --text-muted: #777;
        --brand-blue: #0056b3;
    }

    * {
        box-sizing: border-box;
    }

    html, body {
        background: var(--page-bg);
        color: var(--text-main);
        font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        margin: 0;
        padding: 0;
        min-height: 100vh;
        overflow-x: hidden;
        overflow-y: auto;
    }

    body {
        display: flex;
        flex-direction: column;
    }

    .top-bar {
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
    }

    .top-bar h2 {
        margin: 0;
        font-size: 18px;
        font-weight: 500;
        letter-spacing: 1px;
        line-height: 1.25;
    }

    .top-bar .date {
        font-weight: 700;
        color: #4fc3f7;
        margin-left: 5px;
    }

    .metadata {
        font-size: 12px;
        color: #aaa;
        font-family: monospace;
        display: flex;
        gap: 15px;
        flex-wrap: wrap;
        white-space: nowrap;
    }

    .toolbar {
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
    }

    .control-group {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        flex-wrap: wrap;
    }

    .control-group h3 {
        margin: 0;
        font-size: 11px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-right: 5px;
    }

    .control-label-left {
        margin-left: 15px;
        border-left: 2px solid #ddd;
        padding-left: 15px;
    }

    .main-mode-btn {
        background: transparent;
        border: none;
        font-size: 14px;
        font-weight: 600;
        color: var(--text-muted);
        cursor: pointer;
        padding: 6px 12px;
        border-bottom: 3px solid transparent;
        transition: 0.2s;
    }

    .main-mode-btn:hover {
        color: #111;
    }

    .main-mode-btn.active {
        color: var(--brand-blue);
        border-bottom: 3px solid var(--brand-blue);
    }

    .tab-btn {
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
    }

    .tab-btn:hover {
        background: #e2e6ea;
        color: #111;
    }

    .btn-ir.active {
        background: #d32f2f;
        color: #fff;
        border-color: #b71c1c;
        box-shadow: 0 2px 4px rgba(211,47,47,0.3);
    }

    .btn-vis.active {
        background: #2e7d32;
        color: #fff;
        border-color: #1b5e20;
        box-shadow: 0 2px 4px rgba(46,125,50,0.3);
    }

    .btn-uv.active {
        background: #6a1b9a;
        color: #fff;
        border-color: #4a148c;
        box-shadow: 0 2px 4px rgba(106,27,154,0.3);
    }

    .btn-default.active {
        background: var(--brand-blue);
        color: #fff;
        border-color: #004085;
    }

    .alt-btn.active {
        background: #546e7a;
        color: #fff;
        border-color: #37474f;
    }

    .image-container {
        padding: 15px;
        text-align: center;
        min-height: calc(100vh - 145px);
        display: flex;
        justify-content: center;
        align-items: center;
        flex: 1;
    }

    #main-display {
        max-height: calc(100vh - 175px);
        max-width: 100%;
        object-fit: contain;
        box-shadow: 0 6px 16px rgba(0,0,0,0.15);
        background: #fff;
        cursor: zoom-in;
        transition: opacity 0.2s ease-in-out;
    }

    .image-error {
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
    }

    .image-error.active {
        display: block;
    }

    #myModal {
        display: none;
        position: fixed;
        z-index: 1000;
        inset: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0,0,0,0.9);
        backdrop-filter: blur(5px);
    }

    .modal-close {
        position: absolute;
        top: 15px;
        right: 30px;
        color: #bbb;
        font-size: 40px;
        font-weight: 300;
        cursor: pointer;
        line-height: 1;
    }

    .modal-close:hover {
        color: #fff;
    }

    .modal-content {
        margin: auto;
        display: block;
        max-width: 98%;
        max-height: 95vh;
        margin-top: 1%;
        animation: zoom 0.2s ease-out;
    }

    @keyframes zoom {
        from { transform: scale(0.95); opacity: 0; }
        to { transform: scale(1); opacity: 1; }
    }

    @media (max-width: 900px) {
        .top-bar {
            justify-content: center;
            text-align: center;
            padding: 10px 16px;
        }

        .toolbar {
            gap: 14px;
            padding: 8px 14px;
        }

        .control-label-left {
            margin-left: 0;
            border-left: none;
            padding-left: 0;
        }
    }

    @media (max-width: 640px) {
        .top-bar {
            padding: 10px 12px;
            gap: 6px;
        }

        .top-bar h2 {
            width: 100%;
            font-size: 15px;
            letter-spacing: 0.5px;
        }

        .metadata {
            width: 100%;
            justify-content: center;
            gap: 8px 12px;
            font-size: 10px;
            white-space: normal;
        }

        .toolbar {
            position: static;
            padding: 10px 8px;
            gap: 10px;
        }

        .control-group {
            width: 100%;
            gap: 6px;
        }

        .control-group h3 {
            width: 100%;
            text-align: center;
            margin: 4px 0 2px 0;
            font-size: 10px;
        }

        .main-mode-btn {
            flex: 1 1 135px;
            font-size: 13px;
            padding: 8px 8px;
        }

        .tab-btn {
            flex: 1 1 88px;
            padding: 8px 8px;
            font-size: 11px;
            min-height: 38px;
        }

        .image-container {
            align-items: flex-start;
            padding: 10px 8px 18px 8px;
            min-height: auto;
        }

        #main-display {
            width: 100%;
            max-width: 100%;
            max-height: none;
            height: auto;
            box-shadow: 0 3px 10px rgba(0,0,0,0.18);
        }

        .modal-close {
            top: 10px;
            right: 18px;
            font-size: 34px;
        }

        .modal-content {
            max-width: 100%;
            max-height: 92vh;
            margin-top: 6vh;
        }
    }

    @media (max-width: 390px) {
        .tab-btn {
            flex-basis: 78px;
            font-size: 10px;
            padding-left: 6px;
            padding-right: 6px;
        }

        .main-mode-btn {
            font-size: 12px;
        }
    }
"""


def iter_dashboard_htmls(root: Path):
    for path in root.rglob("*.html"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue

        name = path.name

        if name.endswith("_Dashboard.html"):
            yield path


def ensure_viewport_meta(text: str) -> str:
    if re.search(r'<meta\s+name=["\']viewport["\']', text, flags=re.I):
        return text

    return re.sub(
        r'(<meta\s+charset=["\']utf-8["\']\s*/?>)',
        r'\1\n  <meta name="viewport" content="width=device-width, initial-scale=1">',
        text,
        count=1,
        flags=re.I,
    )


def replace_style_block(text: str) -> str:
    style_re = re.compile(
        r"<style[^>]*>.*?</style>",
        flags=re.I | re.S,
    )

    new_style = f"  <style type=\"text/css\">\n{RESPONSIVE_CSS}\n  </style>"

    if style_re.search(text):
        return style_re.sub(new_style, text, count=1)

    return text.replace("</head>", f"{new_style}\n</head>")


def fix_malformed_span(text: str) -> str:
    return re.sub(r"</span(?!\s*>)", "</span>", text, flags=re.I)


def fix_inline_h3_styles(text: str) -> str:
    text = re.sub(
        r'<h3\s+style="margin-left:\s*15px;\s*border-left:\s*2px\s+solid\s+#ddd;\s*padding-left:\s*15px;">Wavelength</h3>',
        '<h3 class="control-label-left">Wavelength</h3>',
        text,
        flags=re.I,
    )

    text = re.sub(
        r'<h3\s+style="margin-left:\s*10px;">Range</h3>',
        '<h3 class="control-label-left">Range</h3>',
        text,
        flags=re.I,
    )

    return text


def ensure_image_error_box(text: str) -> str:
    if 'id="image-error"' in text or "id='image-error'" in text:
        return text

    img_re = re.compile(
        r'(<img\b[^>]*\bid=["\']main-display["\'][^>]*>)',
        flags=re.I | re.S,
    )

    return img_re.sub(
        r'\1\n      <div id="image-error" class="image-error"></div>',
        text,
        count=1,
    )


def patch_js_onload_onerror(text: str) -> str:
    if "var imageError = document.getElementById(\"image-error\");" not in text:
        text = text.replace(
            'var imgElement = document.getElementById("main-display");',
            'var imgElement = document.getElementById("main-display");\n'
            '    var imageError = document.getElementById("image-error");',
        )

    old_onload_re = re.compile(
        r"imgElement\.onload\s*=\s*function\(\)\s*\{\s*"
        r"imgElement\.style\.opacity\s*=\s*1;\s*"
        r"\};",
        flags=re.I | re.S,
    )

    new_onload = """imgElement.onload = function() {
        imgElement.style.opacity = 1;
        imgElement.style.display = "block";

        if (imageError) {
            imageError.classList.remove("active");
            imageError.textContent = "";
        }
    };

    imgElement.onerror = function() {
        var failedUrl = imgElement.src;

        imgElement.style.opacity = 1;
        imgElement.style.display = "none";

        if (imageError) {
            imageError.textContent =
                "Imagem não encontrada ou falha ao carregar. Verifique se o arquivo existe no R2/GitHub Pages: " +
                failedUrl;

            imageError.classList.add("active");
        }

        console.error("Falha ao carregar:", failedUrl);
    };"""

    if "imgElement.onerror = function()" in text:
        return text

    if old_onload_re.search(text):
        return old_onload_re.sub(new_onload, text, count=1)

    marker = "function setMode(mode)"
    if marker in text:
        return text.replace(marker, new_onload + "\n\n    " + marker)

    return text


def patch_update_image_error_reset(text: str) -> str:
    marker = "function updateImage() {"

    if marker not in text:
        return text

    if 'imageError.classList.remove("active");' in text:
        return text

    replacement = """function updateImage() {
        if (imageError) {
            imageError.classList.remove("active");
            imageError.textContent = "";
        }

        imgElement.style.display = "block";"""

    return text.replace(
        """function updateImage() {
        imgElement.style.opacity = 0.4;""",
        replacement + """
        imgElement.style.opacity = 0.4;""",
    )


def patch_open_modal_guard(text: str) -> str:
    return text.replace(
        "function openModal(src) { modal.style.display = \"block\"; modalImg.src = src; }",
        "function openModal(src) { if (!src || imgElement.style.display === \"none\") return; modal.style.display = \"block\"; modalImg.src = src; }",
    )


def patch_html(text: str) -> str:
    text = ensure_viewport_meta(text)
    text = fix_malformed_span(text)
    text = replace_style_block(text)
    text = fix_inline_h3_styles(text)
    text = ensure_image_error_box(text)
    text = patch_js_onload_onerror(text)
    text = patch_update_image_error_reset(text)
    text = patch_open_modal_guard(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch existing dashboard HTML files without requiring local .webp images."
    )

    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Path to measurements repository root. Default: current directory.",
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Only list changed files.")
    mode.add_argument("--apply", action="store_true", help="Rewrite files in place.")

    args = parser.parse_args()

    root = Path(args.root).resolve()

    changed_files: list[Path] = []

    for html_path in iter_dashboard_htmls(root):
        original = html_path.read_text(encoding="utf-8")
        patched = patch_html(original)

        if patched == original:
            continue

        changed_files.append(html_path)

        rel = html_path.relative_to(root)
        print(f"FIX {rel}")

        if args.apply:
            html_path.write_text(patched, encoding="utf-8")

    if not changed_files:
        print("No dashboard HTML files needed changes.")
        return 0

    if args.dry_run:
        print(f"\n{len(changed_files)} file(s) would be changed.")
        print("Run again with --apply to rewrite them.")
    else:
        print(f"\nUpdated {len(changed_files)} file(s).")
        print("Review with: git diff")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())