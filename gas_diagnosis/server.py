"""Production WSGI application for the gas regulator diagnosis service."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import threading
import time
from uuid import uuid4
from urllib.parse import unquote
import zipfile

from flask import Flask, Response, g, jsonify, request, send_file
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__
from . import web_app as core
from .data_loader import discover_files


LOGGER = logging.getLogger("gas_diagnosis.server")
_CLEANUP_LOCK = threading.Lock()
_LAST_CLEANUP_AT = 0.0


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_enabled(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


class DiagnosisJSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(value):
        try:
            return core._json_default(value)
        except TypeError:
            return DefaultJSONProvider.default(value)


def _cleanup_expired_runtime_files() -> None:
    """Remove expired uploads and report directories at most once per hour."""
    global _LAST_CLEANUP_AT
    retention_days = _env_int("GAS_RETENTION_DAYS", 30, 0, 3650)
    if retention_days == 0:
        return
    now = time.time()
    if now - _LAST_CLEANUP_AT < 3600:
        return
    with _CLEANUP_LOCK:
        if now - _LAST_CLEANUP_AT < 3600:
            return
        cutoff = now - retention_days * 86400
        for root, directories_only in ((core.UPLOAD_DIR, False), (core.REPORT_DIR, True)):
            if not root.exists():
                continue
            for target in root.iterdir():
                if target == core.UPLOAD_LOG or target.stat().st_mtime >= cutoff:
                    continue
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif not directories_only:
                        target.unlink()
                except OSError:
                    LOGGER.warning("runtime_cleanup_failed path=%s", target, exc_info=True)
        _LAST_CLEANUP_AT = now


def _build_reports_zip(reports: list) -> io.BytesIO:
    if not reports:
        raise ValueError("没有可打包的报告")
    if len(reports) > 200:
        raise ValueError("单次最多打包 200 份报告")
    buffer = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(reports, 1):
            if isinstance(item, str):
                path_value = item
                display_name = Path(item).stem
            elif isinstance(item, dict):
                path_value = item.get("path", "")
                display_name = item.get("name") or Path(path_value).stem
            else:
                raise ValueError("报告列表格式无效")
            target = core._report_path_from_link(path_value)
            archive_name = core._safe_zip_name(display_name, index)
            while archive_name in used_names:
                archive_name = core._safe_zip_name(f"{display_name}_{index}", index)
            used_names.add(archive_name)
            archive.write(target, archive_name)
    buffer.seek(0)
    return buffer


def _public_result(result: dict) -> dict:
    result.pop("outputs", None)
    result.pop("source_path", None)
    result.pop("log_path", None)
    return result


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json_provider_class = DiagnosisJSONProvider
    app.json = app.json_provider_class(app)
    app.config.update(
        MAX_CONTENT_LENGTH=_env_int("GAS_MAX_UPLOAD_MB", 50, 1, 500) * 1024 * 1024,
        JSON_AS_ASCII=False,
    )
    if test_config:
        app.config.update(test_config)

    if _env_enabled("GAS_TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    core.REPORT_DIR.mkdir(parents=True, exist_ok=True)

    @app.before_request
    def begin_request() -> None:
        g.request_started_at = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID", "")[:64] or uuid4().hex

    @app.after_request
    def secure_response(response: Response) -> Response:
        response.headers["X-Request-ID"] = getattr(g, "request_id", uuid4().hex)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'self'"
        )
        if request.path.startswith(("/api/", "/file")):
            response.headers["Cache-Control"] = "no-store"
        elif request.path in {"/", "/rules"}:
            response.headers["Cache-Control"] = "no-cache"
        elapsed_ms = (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000
        LOGGER.info(
            "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s remote=%s",
            request.method,
            request.path,
            response.status_code,
            elapsed_ms,
            response.headers["X-Request-ID"],
            request.remote_addr or "-",
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        max_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return jsonify(error=f"上传文件超过 {max_mb} MB 限制"), 413

    @app.errorhandler(FileNotFoundError)
    def missing_file(error):
        return jsonify(error=str(error) or "文件不存在"), 404

    @app.errorhandler(ValueError)
    @app.errorhandler(json.JSONDecodeError)
    def invalid_request(error):
        return jsonify(error=str(error) or "请求参数无效"), 400

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        LOGGER.exception("unhandled_request_error request_id=%s", getattr(g, "request_id", "-"))
        return jsonify(error="服务器处理失败，请携带请求编号联系管理员"), 500

    @app.get("/")
    def index():
        if not core.STATIC_INDEX.exists():
            return Response(core.HTML, mimetype="text/html")
        return send_file(core.STATIC_INDEX, mimetype="text/html", conditional=True, max_age=0)

    @app.get("/rules")
    def rules():
        if not core.STATIC_RULES.exists():
            return Response(core.RULES_HTML, mimetype="text/html")
        return send_file(core.STATIC_RULES, mimetype="text/html", conditional=True, max_age=0)

    @app.get("/healthz")
    def health():
        checks = {
            "baseline": core.DEFAULT_BASELINE.is_file(),
            "data_directory": core.ROOT.is_dir() and os.access(core.ROOT, os.W_OK),
            "report_directory": core.REPORT_DIR.is_dir() and os.access(core.REPORT_DIR, os.W_OK),
        }
        status = "ok" if all(checks.values()) else "degraded"
        return jsonify(status=status, version=__version__, checks=checks), 200 if status == "ok" else 503

    @app.get("/api/files")
    def files():
        if not _env_enabled("GAS_ENABLE_SERVER_FILE_BROWSER"):
            return jsonify(files=[], disabled=True)
        discovered = [
            str(path.relative_to(core.ROOT)).replace("\\", "/")
            for path in discover_files(core.ROOT, sorted(core.ALLOWED_UPLOAD_SUFFIXES))
        ]
        return jsonify(files=discovered)

    @app.get("/api/summary")
    def summary():
        return jsonify(core._summary_payload())

    @app.get("/file")
    def report_file():
        target = core._safe_report_path(request.args.get("path", ""))
        mimetype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return send_file(target, mimetype=mimetype, conditional=True, max_age=0)

    @app.post("/api/diagnose")
    def diagnose_server_path():
        if not _env_enabled("GAS_ENABLE_SERVER_FILE_BROWSER"):
            return jsonify(error="服务器文件诊断接口未启用，请通过上传文件进行诊断"), 403
        payload = request.get_json(force=True) or {}
        target = core._safe_resolve(str(payload.get("path", "")))
        result = core._diagnose_path(target, core._performance_params_from_payload(payload))
        return jsonify(_public_result(result))

    @app.post("/api/llm_analysis")
    def llm_analysis():
        payload = request.get_json(force=True, silent=False) or {}
        result = payload.get("result") or payload
        return jsonify(core._deepseek_analysis(core._compact_llm_result(result)))

    @app.post("/api/reports_zip")
    def reports_zip():
        payload = request.get_json(force=True, silent=False) or {}
        buffer = _build_reports_zip(payload.get("reports") or [])
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name="diagnosis_pdf_reports.zip",
            max_age=0,
        )

    @app.post("/api/upload")
    def upload():
        _cleanup_expired_runtime_files()
        original_name = unquote(request.headers.get("X-Filename", "upload.csv"))
        if len(original_name) > 255:
            raise ValueError("文件名过长")
        raw_params = unquote(request.headers.get("X-Performance-Params", "{}"))
        if len(raw_params) > 16384:
            raise ValueError("判定参数过长")
        performance_params = core._performance_params_from_payload(json.loads(raw_params or "{}"))
        body = request.get_data(cache=False)
        if not body:
            raise ValueError("上传文件为空")
        core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = core.UPLOAD_DIR / core._safe_upload_filename(original_name)
        target.write_bytes(body)
        try:
            result = core._diagnose_path(target, performance_params)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        core._append_upload_log(original_name, target, result)
        result["logged"] = True
        return jsonify(_public_result(result))

    return app


app = create_app()
