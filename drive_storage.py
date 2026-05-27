"""Google Drive upload via service account."""

from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveStorage:
    def __init__(self, config: dict[str, Any], project_root: Optional[Path] = None):
        self._config = config or {}
        self._project_root = project_root or Path(".")
        self._service = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    def _credentials_path(self) -> Path:
        raw = self._config.get("credentials_file", "google_service_account.json")
        path = Path(raw)
        if not path.is_absolute():
            path = self._project_root / path
        return path

    def _get_service(self):
        if self._service is not None:
            return self._service
        creds_path = self._credentials_path()
        if not creds_path.is_file():
            raise FileNotFoundError(f"Google Drive credentials not found: {creds_path}")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            str(creds_path),
            scopes=[DRIVE_SCOPE],
        )
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def upload_file(self, filename: str, data: bytes, mime_type: Optional[str] = None) -> Optional[str]:
        if not self.enabled:
            return None

        folder_id = (self._config.get("folder_id") or "").strip()
        if not folder_id:
            logger.error(
                "Google Drive upload skipped: folder_id is empty. "
                "Create folder '%s', share it with the service account, and set google_drive.folder_id in config.json",
                self._config.get("folder_name", "Telegram_Assistant_Gallery"),
            )
            return None

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or "application/octet-stream"

        try:
            service = self._get_service()
            metadata = {"name": filename, "parents": [folder_id]}
            media = io.BytesIO(data)
            from googleapiclient.http import MediaIoBaseUpload

            upload = MediaIoBaseUpload(media, mimetype=mime_type, resumable=False)
            result = (
                service.files()
                .create(body=metadata, media_body=upload, fields="id,name")
                .execute()
            )
            file_id = result.get("id")
            logger.info("Uploaded to Google Drive: %s (file_id=%s)", filename, file_id)
            return file_id
        except Exception as e:
            logger.error("Google Drive upload failed for %s: %s", filename, e, exc_info=True)
            return None
