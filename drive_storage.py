"""Google Drive upload via OAuth 2.0 (personal Gmail account)."""

from __future__ import annotations

import io
import logging
import mimetypes
import ssl
import time
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_SCOPES = [DRIVE_SCOPE]
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_SIZE = 256 * 1024
UPLOAD_MAX_RETRIES = 3
UPLOAD_RETRY_BASE_DELAY = 2.0


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
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    http = httplib2.Http(timeout=120)
    authorized_http = AuthorizedHttp(credentials, http=http)
    return build("drive", "v3", http=authorized_http, cache_discovery=False)


def _is_retryable_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, (ssl.SSLError, ConnectionError, ConnectionResetError, BrokenPipeError)):
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "eof occurred",
            "connection reset",
            "broken pipe",
            "timed out",
            "temporary failure",
            "server disconnected",
        )
    )


def _execute_resumable_upload(request) -> dict:
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.debug("Google Drive upload progress: %.0f%%", status.progress() * 100)
    return response


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

    def _reset_service(self) -> None:
        self._service = None

    def upload_file(
        self,
        filename: str,
        data: Optional[bytes] = None,
        mime_type: Optional[str] = None,
        *,
        filepath: Optional[Union[Path, str]] = None,
    ) -> Optional[str]:
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

        if filepath is not None:
            source_path = Path(filepath)
            if not source_path.is_file():
                logger.error("Google Drive upload skipped: file not found: %s", source_path)
                return None
        elif data is not None:
            source_path = None
        else:
            logger.error("Google Drive upload skipped: neither data nor filepath provided")
            return None

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or "application/octet-stream"

        metadata = {"name": filename, "parents": [folder_id]}
        last_error: Optional[Exception] = None

        for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
            try:
                from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

                service = self._get_service()
                if source_path is not None:
                    media = MediaFileUpload(
                        str(source_path),
                        mimetype=mime_type,
                        resumable=True,
                        chunksize=UPLOAD_CHUNK_SIZE,
                    )
                else:
                    media = MediaIoBaseUpload(
                        io.BytesIO(data),
                        mimetype=mime_type,
                        resumable=True,
                        chunksize=UPLOAD_CHUNK_SIZE,
                    )

                request = service.files().create(
                    body=metadata,
                    media_body=media,
                    fields="id,name",
                )
                result = _execute_resumable_upload(request)
                file_id = result.get("id")
                logger.info("Uploaded to Google Drive: %s (file_id=%s)", filename, file_id)
                return file_id
            except Exception as e:
                last_error = e
                if attempt >= UPLOAD_MAX_RETRIES or not _is_retryable_upload_error(e):
                    break
                delay = UPLOAD_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Google Drive upload attempt %s/%s failed for %s: %s; retry in %.1fs",
                    attempt,
                    UPLOAD_MAX_RETRIES,
                    filename,
                    e,
                )
                self._reset_service()
                time.sleep(delay)

        logger.error(
            "Google Drive upload failed for %s: %s",
            filename,
            last_error,
            exc_info=last_error,
        )
        return None
