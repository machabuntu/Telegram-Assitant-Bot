"""Lightweight HTTP healthcheck server for the Telegram bot process."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_START_TIME = time.monotonic()
_SERVICE_NAME = "telegram-assistant-bot"


def _normalize_path(path: str) -> str:
    parsed = urlparse(path)
    normalized = parsed.path.rstrip("/") or "/"
    return normalized


def _check_database() -> str:
    try:
        import database

        conn = database.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            if row and row[0] == 1:
                return "ok"
            return "error"
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Healthcheck database ping failed: %s", e)
        return "error"


def _build_handler(health_path: str, check_db: bool):
    expected_path = _normalize_path(health_path)

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if _normalize_path(self.path) != expected_path:
                self.send_error(404)
                return

            db_status = "skipped"
            if check_db:
                db_status = _check_database()

            body = {
                "status": "alive",
                "service": _SERVICE_NAME,
                "uptime_seconds": int(time.monotonic() - _START_TIME),
                "database": db_status,
            }
            payload = json.dumps(body).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:
            logger.debug("Healthcheck %s - %s", self.address_string(), format % args)

    return HealthHandler


def start_health_server(
    host: str,
    port: int,
    path: str = "/healthz",
    *,
    check_db: bool = True,
) -> ThreadingHTTPServer:
    """Start healthcheck HTTP server in a daemon thread."""
    handler = _build_handler(path, check_db)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="healthcheck", daemon=True)
    thread.start()
    logger.info("Healthcheck listening on http://%s:%s%s", host, port, path)
    return server
