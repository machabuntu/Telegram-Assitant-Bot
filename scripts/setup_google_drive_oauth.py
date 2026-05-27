#!/usr/bin/env python3
"""
One-time Google Drive OAuth setup (run on a machine with a browser).

Usage:
    python scripts/setup_google_drive_oauth.py

Reads google_drive section from config.json, opens browser for Google login,
saves refresh token to token_file, creates gallery folder if folder_id is empty.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drive_storage import (  # noqa: E402
    DRIVE_SCOPES,
    build_drive_service,
    create_gallery_folder,
    resolve_project_path,
    save_credentials,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup_google_drive_oauth")


def load_drive_config() -> dict:
    config_path = ROOT / "config.json"
    if not config_path.is_file():
        log.error("config.json not found in project root: %s", ROOT)
        sys.exit(1)
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    drive_cfg = cfg.get("google_drive")
    if not isinstance(drive_cfg, dict):
        log.error("config.json must contain a 'google_drive' section")
        sys.exit(1)
    return drive_cfg


def main() -> None:
    drive_cfg = load_drive_config()
    oauth_client_path = resolve_project_path(
        ROOT, drive_cfg.get("oauth_client_file", "google_oauth_client.json")
    )
    token_path = resolve_project_path(
        ROOT, drive_cfg.get("token_file", "google_drive_token.json")
    )
    folder_name = drive_cfg.get("folder_name", "Telegram_Assistant_Gallery")
    folder_id = (drive_cfg.get("folder_id") or "").strip()

    if not oauth_client_path.is_file():
        log.error(
            "OAuth client JSON not found: %s\n"
            "Download Desktop app credentials from Google Cloud Console and save as this file.",
            oauth_client_path,
        )
        sys.exit(1)

    log.info("Starting OAuth flow (browser will open)...")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(oauth_client_path), DRIVE_SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    save_credentials(token_path, credentials)
    log.info("Token saved: %s", token_path)

    service = build_drive_service(credentials)

    if not folder_id:
        log.info("Creating gallery folder '%s' via Drive API...", folder_name)
        folder_id = create_gallery_folder(service, folder_name)
        log.info("Created folder: %s (id=%s)", folder_name, folder_id)
    else:
        log.info("Using existing folder_id from config: %s", folder_id)

    print("\n" + "=" * 60)
    print("Google Drive OAuth setup complete.")
    print("=" * 60)
    print(f"\nToken file:     {token_path}")
    print(f"OAuth client:   {oauth_client_path}")
    print(f"\nAdd to config.json → google_drive.folder_id:\n")
    print(f'  "folder_id": "{folder_id}"')
    print("\nCopy to VPS (along with config.json):")
    print(f"  - {oauth_client_path.name}")
    print(f"  - {token_path.name}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
