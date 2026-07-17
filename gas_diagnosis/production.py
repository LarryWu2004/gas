"""Waitress production entry point."""

from __future__ import annotations

import logging
import os

from waitress import serve

from .pdf_report import find_chromium
from .server import app


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GAS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("GAS_DIAGNOSIS_HOST", "0.0.0.0")
    port = _env_int("GAS_DIAGNOSIS_PORT", 8080, 1, 65535)
    threads = _env_int("GAS_SERVER_THREADS", 8, 2, 64)
    max_body_size = _env_int("GAS_MAX_UPLOAD_MB", 50, 1, 500) * 1024 * 1024
    pdf_renderer = find_chromium()
    logging.getLogger(__name__).info(
        "starting_production_server host=%s port=%s threads=%s data_dir=%s pdf_renderer=%s",
        host,
        port,
        threads,
        os.environ.get("GAS_DATA_DIR", "project root"),
        pdf_renderer,
    )
    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        ident="GasDiagnosisServer",
        max_request_body_size=max_body_size,
        channel_timeout=300,
        expose_tracebacks=False,
    )


if __name__ == "__main__":
    main()
