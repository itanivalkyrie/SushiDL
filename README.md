<p align="center">
  <img alt="SushiDL banner" src="assets/banner.png" />
</p>

# SushiDL - Manga Downloader with GUI

SushiDL is a Python application with a Tkinter interface to download manga chapters/volumes from:
- https://sushiscan.fr
- https://sushiscan.net
- https://mangas-origines.fr
- https://hentai-origines.fr (adult content)

Current version: `11.2.8`

## What is new (11.2.8)

- Faster worker loops (removed artificial waits in download flow).
- Better cancellation during image extraction retries.
- Safety timeout added on synchronous UI calls (`run_on_ui(wait=True)`).
- Lower thread contention in logs (removed per-image success log spam).
- Stronger cookie/header sanitization before HTTP requests.
- Cookie validity probe now always uses fixed startup URLs per domain.

## Main features

- Manual authentication only (`cf_clearance` + `User-Agent`).
- Separate cookie fields for `.fr`, `.net`, `.origines`, `.hentai-origines`.
- Automatic domain mapping for requests, image downloads, and cover fetch.
- Multi-thread image download with retries and failure classification.
- Optional WebP to JPG conversion.
- Optional CBZ archive generation.
- Smart resume for already downloaded pages.
- Download cancel support at runtime.
- Unified logs in GUI and terminal (with filters).
- Error table per volume (stage, HTTP code, reason, suggested action).

## Requirements

- Python 3.10+
- Dependencies from `requirements.txt`

Windows:

```bash
python --version
```

Linux (Debian/Ubuntu):

```bash
sudo apt update
sudo apt install python3 python3-pip python3-tk
python3 --version
```

## Installation

```bash
git clone https://github.com/itanivalkyrie/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

## Run

Windows:

```bash
python SushiDL.py
```

Linux:

```bash
python3 SushiDL.py
```

## Authentication setup (manual mode)

SushiDL does not use FlareSolverr/Playwright/browser import in the main flow.
You must provide:
- one `cf_clearance` cookie per domain you use
- one valid `User-Agent`

Quick setup:
1. Open the target site in your browser and pass Cloudflare challenge if needed.
2. Copy `cf_clearance` for the matching domain.
3. Get your user-agent (for example from `https://httpbin.org/user-agent`).
4. Paste values into SushiDL fields and save settings.

## Configuration files

- `config.json`: global app settings and helper links.
- `cookie_cache.json`: persisted runtime preferences and auth values.

Default `config.json` schema:

```json
{
  "auth_mode": "manual",
  "manual_links": {
    "cookie_fr": "https://sushiscan.fr",
    "cookie_net": "https://sushiscan.net",
    "cookie_origines": "https://mangas-origines.fr",
    "cookie_hentai": "https://hentai-origines.fr",
    "user_agent": "https://httpbin.org/user-agent",
    "cookie_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-recuperer-user-agent-et-cf_clearance"
  }
}
```

## Typical workflow

1. Launch `SushiDL.py`.
2. Fill cookies and user-agent in the Authentication tab.
3. Paste a supported catalog URL.
4. Click Analyze.
5. Select chapters/volumes.
6. Click Download and choose output folder.

## Output

By default, downloads are created under `DL SushiScan/` (or your selected output directory).

Typical structure:

```text
<output_root>/
  <manga_title>/
    <manga_title> - <tome_or_chapter>.cbz
```

If CBZ is disabled, images are kept in per-volume folders.

## Troubleshooting

- HTTP 403 / challenge page: refresh `cf_clearance` and check `User-Agent`.
- Empty chapter list: verify source URL format and domain cookie.
- Download errors: retry later for 429/5xx, or update auth data.

## Optional helper tool

`tools/remove_last_images_cbz.py` can remove trailing ad/parasite pages from CBZ files in batch mode.

## Project layout

- `SushiDL.py`: main app
- `legacy_scripts/SushiDL_V9.py`: legacy version
- `tools/remove_last_images_cbz.py`: CBZ cleanup tool
- `cut_sushiscan_fr/`: image split/rebuild scripts
- `CHANGELOG.md`: release history

## Changelog

See `CHANGELOG.md` for release-by-release history.

## Support

If this project is useful to you, you can support the maintainer on Ko-fi:
- https://ko-fi.com/itanivalkyrie
