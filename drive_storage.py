"""Google Drive upload via OAuth 2.0 (personal Gmail account)."""

from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_SCOPES = [DRIVE_SCOPE]
FOLDER_MIME = "application/vnd.google-apps.folder"


def resolve_project_path(project_root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path


def save_credentials(token_path: Path, credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")


def load_oauth_credentials(
    oauth_client_path: Path,
    token_path: Path,
    *,
    refresh: bool = True,
) -> Optional[Any]:
    """Load OAuth user credentials from token file; refresh access token if expired."""
    if not token_path.is_file():
        logger.error(
            "Google Drive token not found: %s. Run: python scripts/setup_google_drive_oauth.py",
            token_path,
        )
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google.auth.exceptions import RefreshError

    credentials = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)

    if not refresh:
        return credentials

    if credentials.expired and credentials.refresh_token:
        if not oauth_client_path.is_file():
            logger.error(
                "Google Drive OAuth client file not found: %s (needed to refresh token)",
                oauth_client_path,
            )
            return None
        try:
            credentials.refresh(Request())
            save_credentials(token_path, credentials)
            logger.info("Google Drive access token refreshed")
        except RefreshError as e:
            logger.error(
                "Google Drive token refresh failed: %s. Re-run: python scripts/setup_google_drive_oauth.py",
                e,
            )
            return None

    if not credentials.valid:
        logger.error(
            "Google Drive credentials invalid. Re-run: python scripts/setup_google_drive_oauth.py"
        )
        return None

    return credentials


def build_drive_service(credentials):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def create_gallery_folder(service, folder_name: str) -> str:
    """Create a Drive folder owned by the authenticated user; returns folder_id."""
    result = (
        service.files()
        .create(
            body={"name": folder_name, "mimeType": FOLDER_MIME},
            fields="id,name",
        )
        .execute()
    )
    return result["id"]


class DriveStorage:
    def __init__(self, config: dict[str, Any], project_root: Optional[Path] = None):
        self._config = config or {}
        self._project_root = project_root or Path(".")
        self._service = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    def _oauth_client_path(self) -> Path:
        raw = self._config.get("oauth_client_file", "google_oauth_client.json")
        return resolve_project_path(self._project_root, raw)

    def _token_path(self) -> Path:
        raw = self._config.get("token_file", "google_drive_token.json")
        return resolve_project_path(self._project_root, raw)

    def _get_credentials(self):
        return load_oauth_credentials(self._oauth_client_path(), self._token_path())

    def _get_service(self):
        if self._service is not None:
            return self._service
        credentials = self._get_credentials()
        if credentials is None:
            raise FileNotFoundError("Google Drive OAuth credentials are not configured")
        self._service = build_drive_service(credentials)
        return self._service

    def upload_file(self, filename: str, data: bytes, mime_type: Optional[str] = None) -> Optional[str]:
        if not self.enabled:
            return None

        folder_id = (self._config.get("folder_id") or "").strip()
        if not folder_id:
            logger.error(
                "Google Drive upload skipped: folder_id is empty. "
                "Run scripts/setup_google_drive_oauth.py and set google_drive.folder_id in config.json "
                "(folder: %s)",
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
