"""Local web UI for the gas regulator diagnosis MVP."""

from __future__ import annotations

import io
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4
import zipfile

import pandas as pd

from .cli import diagnose_frame
from .data_loader import discover_files, inspect_pressure_file, load_pressure_file
from .features import resample_curve
from .report import write_reports


def _curve_points_for_display(data: pd.DataFrame) -> int:
    count = int(pd.to_numeric(data.get("pressure_kpa"), errors="coerce").dropna().size)
    if count <= 1:
        return 2
    return min(count, 6000)


# ── 路径解析（支持本地运行、容器部署与 PyInstaller 打包） ─────────
def _resolve_paths() -> tuple[Path, Path]:
    """返回 (bundle_dir, data_dir)
    - bundle_dir: 只读资源目录（静态文件、模型等）
    - data_dir: 用户可写数据目录（上传、诊断输出等）
    """
    root = Path(__file__).resolve().parents[1]
    bundle_dir = Path(os.environ.get("GAS_BUNDLE_DIR", root)).expanduser()
    data_dir = Path(os.environ.get("GAS_DATA_DIR", root)).expanduser()
    return bundle_dir.resolve(), data_dir.resolve()


_BUNDLE_DIR, _DATA_DIR = _resolve_paths()

ROOT = _DATA_DIR  # 兼容 _safe_resolve 和 discover_files
STATIC_INDEX = _BUNDLE_DIR / "gas_diagnosis" / "static" / "index.html"
STATIC_RULES = _BUNDLE_DIR / "gas_diagnosis" / "static" / "rules.html"
DEFAULT_BASELINE = _BUNDLE_DIR / "models" / "baseline_healthy.json"
UPLOAD_DIR = _DATA_DIR / "outputs" / "web_uploads"
REPORT_DIR = _DATA_DIR / "outputs" / "web_diagnosis"
UPLOAD_LOG = REPORT_DIR / "upload_diagnosis_log.csv"
LOG_LOCK = threading.Lock()
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xls"}
ALLOWED_REPORT_SUFFIXES = {".pdf", ".html", ".json", ".md"}


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _safe_resolve(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if ROOT not in resolved.parents and resolved != ROOT:
        raise ValueError("path outside workspace is not allowed")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    resolved_root = root.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _safe_upload_filename(name: str) -> str:
    original = Path(str(name or "upload.csv")).name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise ValueError("仅支持 CSV、XLSX 或 XLS 文件")
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in Path(original).stem)
    stem = stem.strip("_")[:80] or "upload"
    return f"{uuid4().hex}_{stem}{suffix}"


def _load_baseline() -> dict:
    if not DEFAULT_BASELINE.exists():
        raise FileNotFoundError(f"baseline not found: {DEFAULT_BASELINE}")
    return json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))


def _performance_params_from_payload(payload: dict | None) -> dict:
    payload = payload or {}
    params = payload.get("performance_params") or payload
    keys = (
        "set_pressure_kpa",
        "ac_percent",
        "sg_percent",
        "extreme_sample_n",
        "seat_leak_limit",
        "seat_leak_surrogate_limit_kpa",
        "ac_kpa",
        "sg_kpa",
        "seat_leak_limit_kpa",
    )
    parsed = {}
    for key in keys:
        value = params.get(key)
        if value in (None, ""):
            continue
        parsed[key] = float(value)
    if params.get("seat_leak_unit"):
        parsed["seat_leak_unit"] = str(params.get("seat_leak_unit"))
    return parsed


def _report_path_from_link(value: str) -> Path:
    value = str(value or "")
    if value.startswith("/file"):
        parsed = urlparse(value)
        value = parse_qs(parsed.query).get("path", [""])[0]
    target = _safe_resolve(unquote(value))
    if target.suffix.lower() != ".pdf":
        raise ValueError("only PDF reports can be packaged")
    if not _is_within(target, REPORT_DIR):
        raise ValueError("report path outside report directory is not allowed")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"report not found: {target}")
    return target


def _safe_report_path(value: str) -> Path:
    target = _safe_resolve(unquote(str(value or "")))
    if not _is_within(target, REPORT_DIR):
        raise ValueError("report path outside report directory is not allowed")
    if target.suffix.lower() not in ALLOWED_REPORT_SUFFIXES:
        raise ValueError("unsupported report type")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError("report not found")
    return target


def _public_report_link(value: str | Path) -> str:
    target = Path(value).resolve()
    relative = target.relative_to(ROOT.resolve()).as_posix()
    return "/file?path=" + quote(relative, safe="/")


def _safe_zip_name(name: str, index: int) -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name or f"report_{index}"))
    stem = stem.strip("_") or f"report_{index}"
    return f"{index:02d}_{stem[:80]}.pdf"


def _diagnose_path(path: Path, performance_params: dict | None = None) -> dict:
    input_profile = inspect_pressure_file(path)
    data = load_pressure_file(path)
    result = diagnose_frame(data, _load_baseline(), performance_params)
    result["curve"] = resample_curve(data, _curve_points_for_display(data))
    result["input_profile"] = input_profile
    station = "".join(ch if ch.isalnum() else "_" for ch in result["features"]["station"])
    output_dir = REPORT_DIR / f"{uuid4().hex}_{station[:60]}"
    outputs = write_reports(result, data, output_dir)
    result["outputs"] = outputs
    result["source_path"] = str(path.resolve().relative_to(ROOT.resolve()))
    result["report_links"] = {
        key: _public_report_link(value)
        for key, value in outputs.items()
        if key in {"pdf", "overview_pdf"}
    }
    return result


def _compact_llm_result(result: dict) -> dict:
    """Keep only diagnosis facts needed by the external analysis model."""
    result = result or {}
    features = result.get("features") or {}
    health = result.get("health") or {}
    performance = result.get("performance") or {}
    profile = result.get("input_profile") or {}

    def compact_item(item: dict) -> dict:
        return {
            "code": item.get("code"),
            "name": item.get("name"),
            "level": item.get("level"),
            "label": item.get("label"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "ratio": item.get("ratio"),
            "reference": item.get("reference"),
            "confidence": item.get("confidence"),
            "description": item.get("description"),
        }

    return {
        "features": {
            "station": features.get("station"),
            "start": features.get("start"),
            "end": features.get("end"),
            "count": features.get("count"),
            "mean": features.get("mean"),
            "min": features.get("min"),
            "max": features.get("max"),
            "std": features.get("std"),
        },
        "health": {
            "level": health.get("level"),
            "label": health.get("label"),
            "risk_score": health.get("risk_score"),
        },
        "performance": {
            "params": performance.get("params") or {},
            "overall": performance.get("overall") or {},
            "items": [compact_item(item) for item in performance.get("items", [])],
        },
        "findings": [
            {
                "name": item.get("name"),
                "severity": item.get("severity"),
                "maintenance": item.get("maintenance"),
            }
            for item in result.get("findings", [])
        ],
        "input_profile": {
            "file_name": profile.get("file_name") or result.get("source_filename"),
            "file_type": profile.get("file_type"),
            "valid_rows": profile.get("valid_rows"),
            "invalid_rows": profile.get("invalid_rows"),
            "warnings": profile.get("warnings") or [],
        },
    }


def _sanitize_llm_content(content: str) -> str:
    """Keep user-facing analysis free of internal model and feature names."""
    text = str(content or "")
    replacements = (
        (r"KNN\s*异常信号", "辅助异常信号"),
        (r"(?<![A-Za-z0-9_])KNN(?![A-Za-z0-9_])", "辅助分析"),
        (r"(?<![A-Za-z0-9_])wave[_\s-]*count(?![A-Za-z0-9_])", "压力波动"),
        (r"(?<![A-Za-z0-9_])Isolation\s*Forest(?![A-Za-z0-9_])", "辅助分析"),
        (r"(?<![A-Za-z])IF(?![A-Za-z])", "辅助分析"),
        (r"(?<![A-Za-z0-9_])baseline[_\s-]*score(?![A-Za-z0-9_])", "历史偏离程度"),
        (r"(?<![A-Za-z0-9_])top[_\s-]*features(?![A-Za-z0-9_])", "主要影响因素"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return re.sub(r"辅助分析(?:层)?辅助分析", "辅助分析", text).strip()


def _local_llm_analysis(summary: dict, reason: str = "") -> dict:
    health = summary.get("health") or {}
    performance = summary.get("performance") or {}
    features = summary.get("features") or {}
    items = performance.get("items") or []
    worst_items = sorted(items, key=lambda x: int(x.get("level") or 0), reverse=True)[:2]
    focus = "、".join(
        f"{item.get('code', '')}{item.get('name', '')}{item.get('level', '—')}级"
        for item in worst_items
    ) or "核心性能指标"
    findings = summary.get("findings") or []
    reason = ""
    if findings:
        top = sorted(findings, key=lambda x: int(x.get("severity") or 0), reverse=True)[0]
        reason = f"，主要触发{top.get('code')}{top.get('name')}"
    lines = [
        f"结论：当前为{health.get('level', '—')}级（{health.get('label', '—')}），主要关注{focus}{reason}。",
        f"建议：优先复核等级最高的性能项和对应压力曲线片段；结合现场工况确认参数设置是否匹配。数据范围为{features.get('start', '—')}至{features.get('end', '—')}，有效样本{features.get('count', '—')}条。",
    ]
    return {
        "ok": True,
        "provider": "local_template",
        "fallback_reason": reason,
        "content": "\n".join(lines),
    }


def _deepseek_analysis(summary: dict) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip().lstrip("\ufeff")
    key_file = Path(os.environ.get("DEEPSEEK_API_KEY_FILE", _DATA_DIR / "deepseek_api_key.txt"))
    if not api_key and key_file.exists():
        api_key = key_file.read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")
    if not api_key:
        return _local_llm_analysis(summary, "未配置 DEEPSEEK_API_KEY，已使用本地模板生成。")

    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
    env_model = os.environ.get("DEEPSEEK_MODEL", "").strip().lstrip("\ufeff")
    model_file = _DATA_DIR / "deepseek_model.txt"
    if not env_model and model_file.exists():
        env_model = model_file.read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")
    models = [env_model] if env_model else []
    for candidate in ("deepseek-v4-flash", "deepseek-chat"):
        if candidate not in models:
            models.append(candidate)
    system_prompt = (
        "你是燃气调压器诊断结论提炼助手。"
        "只能基于用户提供的JSON事实生成中文分析意见，不得自行修改P01/P02/P03等级、综合等级或风险分。"
        "目标是直击重点，不复述完整报告。"
        "输出纯文本，不要Markdown、不要星号、不要表格、不要标题装饰。"
        "总字数控制在160字以内。"
        "固定格式只能包含两项：结论：...；建议：...。"
        "结论只写最终状态和最主要原因。建议只写下一步处置动作。"
        "不要逐项复述全部指标，不要单独强调非实测、估算、模拟等字样；除非它直接影响处置动作，最多轻描淡写一句复核即可。"
        "不得引用算法名称、英文变量名、内部字段名或特征名。"
        "不要出现KNN、wave_count、Isolation Forest、IF、baseline_score、top_features等词语，"
        "只使用稳压性能、关闭压力性能、阀座密封性能、压力波动、风险等级等易懂的业务表述。"
    )
    user_prompt = (
        "请基于以下诊断JSON生成极简重点分析。只输出结论和建议两项。\n\n"
        + json.dumps(summary, ensure_ascii=False, default=_json_default)
    )
    last_reason = ""
    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 1200,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if not content:
                raise RuntimeError("DeepSeek 返回内容为空")
            return {
                "ok": True,
                "provider": "deepseek",
                "model": model,
                "content": _sanitize_llm_content(content),
                "usage": data.get("usage"),
            }
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(exc)
            last_reason = f"DeepSeek HTTP {exc.code}: {detail[:240]}"
        except Exception as exc:  # noqa: BLE001
            last_reason = f"DeepSeek 调用失败: {exc}"
    return _local_llm_analysis(summary, last_reason)


def _summary_rows(summary_csv: Path, source: str) -> pd.DataFrame:
    columns = ["health_level", "health_label", "source"]
    if not summary_csv.exists():
        return pd.DataFrame(columns=columns)
    rows = []
    with summary_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return pd.DataFrame(columns=columns)
        if "health_level" not in header or "health_label" not in header:
            return pd.DataFrame(columns=columns)
        level_idx = header.index("health_level")
        label_idx = header.index("health_label")
        for row in reader:
            if len(row) <= max(level_idx, label_idx):
                continue
            rows.append({"health_level": row[level_idx], "health_label": row[label_idx], "source": source})
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def _summary_payload() -> dict:
    batch_summary = ROOT / "outputs" / "diagnosis_batch_healthy" / "batch_diagnosis_summary.csv"
    batch_rows = _summary_rows(batch_summary, "history")
    upload_rows = _summary_rows(UPLOAD_LOG, "upload")
    df = pd.concat([batch_rows, upload_rows], ignore_index=True)
    if df.empty:
        return {
            "health_distribution": [],
            "summary_totals": {"history": 0, "upload": 0, "total": 0},
        }

    grouped = (
        df.groupby(["health_level", "health_label"])
        .size()
        .reset_index(name="count")
        .sort_values("health_level")
    )
    source_counts = df.groupby("source").size().to_dict()
    return {
        "health_distribution": grouped.to_dict(orient="records"),
        "summary_totals": {
            "history": int(source_counts.get("history", 0)),
            "upload": int(source_counts.get("upload", 0)),
            "total": int(len(df)),
        },
    }


def _append_upload_log(original_name: str, saved_path: Path, result: dict) -> None:
    features = result.get("features", {})
    health = result.get("health", {})
    ai = result.get("ai", {})
    isolation = ai.get("isolation_forest") or {}
    knn = ai.get("knn") or {}
    performance = result.get("performance") or {}
    perf_items = {item.get("code"): item for item in performance.get("items", [])}
    outputs = result.get("outputs", {})
    findings = result.get("findings") or []
    row = {
        "logged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "original_filename": original_name,
        "saved_path": str(saved_path),
        "station": features.get("station"),
        "health_level": health.get("level"),
        "health_label": health.get("label"),
        "diagnosis_mode": health.get("diagnosis_mode"),
        "risk_score": health.get("risk_score"),
        "anomaly_score": health.get("ai_score"),
        "baseline_score": ai.get("score"),
        "baseline_deviation_score": ai.get("baseline_score"),
        "model_anomaly_score": isolation.get("score"),
        "model_raw_score": isolation.get("raw_score"),
        "model_band": isolation.get("band_label"),
        "model_percentile": isolation.get("percentile"),
        "knn_score": knn.get("score"),
        "knn_raw_score": knn.get("raw_score"),
        "knn_band": knn.get("band_label"),
        "knn_percentile": knn.get("percentile"),
        "performance_level": (performance.get("overall") or {}).get("level"),
        "performance_label": (performance.get("overall") or {}).get("label"),
        "steady_level": (perf_items.get("P01") or {}).get("level"),
        "steady_ratio": (perf_items.get("P01") or {}).get("ratio"),
        "closing_level": (perf_items.get("P02") or {}).get("level"),
        "closing_ratio": (perf_items.get("P02") or {}).get("ratio"),
        "seat_seal_level": (perf_items.get("P03") or {}).get("level"),
        "seat_seal_ratio": (perf_items.get("P03") or {}).get("ratio"),
        "findings": ";".join(f"{item.get('code')}:{item.get('name')}" for item in findings),
        "start": features.get("start"),
        "end": features.get("end"),
        "count": features.get("count"),
        "mean": features.get("mean"),
        "min": features.get("min"),
        "max": features.get("max"),
        "report_html": outputs.get("html"),
        "report_markdown": outputs.get("markdown"),
        "report_json": outputs.get("json"),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK:
        exists = UPLOAD_LOG.exists()
        encoding = "utf-8" if exists else "utf-8-sig"
        pd.DataFrame([row]).to_csv(UPLOAD_LOG, mode="a", header=not exists, index=False, encoding=encoding)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>燃气调压器健康诊断</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f5f8;
      --panel: #ffffff;
      --ink: #111827;
      --muted: #6b7280;
      --soft: #eef2f7;
      --line: #d7dde7;
      --blue: #1f5fbf;
      --blue-dark: #174a99;
      --red: #b91c1c;
      --amber: #b45309;
      --green: #047857;
      --shadow: 0 12px 30px rgba(15, 23, 42, .06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      background: #edf2f7;
      color: var(--ink);
    }
    header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #fff;
      box-shadow: 0 1px 0 rgba(15, 23, 42, .02);
    }
    .header-inner {
      max-width: 1280px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 23px;
      letter-spacing: 0;
      color: #0f172a;
    }
    .nav-link {
      color: var(--blue);
      text-decoration: none;
      font-weight: 700;
      border: 1px solid #c8d8f0;
      background: #f8fbff;
      border-radius: 6px;
      padding: 8px 12px;
      white-space: nowrap;
    }
    .nav-link:hover {
      border-color: #93a8c6;
      background: #eef6ff;
    }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: minmax(320px, 360px) 1fr;
      gap: 20px;
      align-items: start;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      box-shadow: var(--shadow);
    }
    h2 {
      margin: 0 0 14px;
      font-size: 17px;
      color: #111827;
    }
    label {
      display: block;
      margin: 13px 0 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }
    select, input[type="text"], input[type="file"], input[type="number"] {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    select:focus, input[type="text"]:focus, input[type="file"]:focus, input[type="number"]:focus {
      border-color: #86aee8;
      box-shadow: 0 0 0 3px rgba(31, 95, 191, .12);
    }
    select {
      background: linear-gradient(#fff, #fbfcfe);
    }
    button {
      min-height: 40px;
      border: 1px solid var(--blue);
      background: var(--blue);
      color: #fff;
      border-radius: 6px;
      padding: 8px 14px;
      cursor: pointer;
      font-weight: 600;
      transition: background .15s ease, border-color .15s ease, transform .05s ease;
    }
    button:hover {
      background: var(--blue-dark);
      border-color: var(--blue-dark);
    }
    button:active {
      transform: translateY(1px);
    }
    button.secondary {
      background: #fff;
      color: var(--blue);
      border-color: #b8c7dc;
    }
    button.secondary:hover {
      background: #f8fafc;
      border-color: #93a8c6;
    }
    button:disabled {
      opacity: .55;
      cursor: wait;
    }
    .row {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .status {
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
      min-height: 18px;
    }
    .format-hint {
      margin-top: 8px;
      padding: 10px 12px;
      border: 1px solid #c8d8f0;
      border-radius: 8px;
      background: #f4f8ff;
      color: #29466f;
      font-size: 12px;
      line-height: 1.55;
    }
    .format-hint strong {
      display: block;
      margin-bottom: 4px;
      color: #1e40af;
      font-size: 13px;
    }
    .format-hint ul {
      margin: 0;
      padding-left: 17px;
    }
    .format-hint li {
      margin: 2px 0;
    }
    .param-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }
    .param-grid label {
      margin: 0 0 4px;
    }
    .param-grid input {
      margin: 0;
    }
    .param-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .quality {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fbfcfe;
      margin-bottom: 16px;
    }
    .quality h3 {
      margin: 0 0 10px;
      font-size: 15px;
    }
    .warnings {
      margin-top: 10px;
      color: var(--amber);
      font-size: 13px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .result-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
      margin-bottom: 16px;
    }
    .result-head .eyebrow {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .result-head .title {
      font-size: 22px;
      font-weight: 800;
      line-height: 1.25;
    }
    .result-head .meta {
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      line-height: 1.5;
    }
    .core-grid {
      display: grid;
      grid-template-columns: 1.2fr repeat(3, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px 14px;
      background: linear-gradient(180deg, #ffffff 0%, #fbfcfe 100%);
      min-height: 82px;
    }
    .metric.primary {
      background: #0f766e;
      border-color: #0f766e;
      color: #fff;
    }
    .metric.primary .label,
    .metric.primary .subvalue {
      color: rgba(255, 255, 255, .82);
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
      line-height: 1.35;
    }
    .metric .value {
      font-size: 22px;
      font-weight: 700;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .metric .subvalue {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .perf-card.level-1 .value { color: var(--green); }
    .perf-card.level-2 .value { color: #2563eb; }
    .perf-card.level-3 .value { color: var(--amber); }
    .perf-card.level-4 .value, .perf-card.level-5 .value { color: var(--red); }
    .level-1 .value { color: var(--green); }
    .level-2 .value { color: #2563eb; }
    .level-3 .value { color: var(--amber); }
    .level-4 .value, .level-5 .value { color: var(--red); }
    .section-title {
      margin: 18px 0 10px;
      font-size: 16px;
      font-weight: 800;
    }
    .aux-block {
      border-top: 1px solid var(--line);
      margin-top: 18px;
      padding-top: 16px;
    }
    .aux-block h3 {
      margin: 16px 0 10px;
      font-size: 14px;
    }
    .aux-note {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 9px 8px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      background: #f8fafc;
    }
    tr:last-child th, tr:last-child td { border-bottom: 0; }
    .links a {
      display: inline-block;
      margin: 0 10px 8px 0;
      color: var(--blue);
      text-decoration: none;
      font-weight: 600;
    }
    .links a:hover {
      text-decoration: underline;
    }
    .placeholder {
      color: var(--muted);
      padding: 56px 12px;
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }
    .chart {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      margin-top: 14px;
      padding: 8px;
    }
    @media (max-width: 900px) {
      header { padding: 16px; }
      main { grid-template-columns: 1fr; padding: 16px; }
      .metrics { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
      .core-grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .result-head { flex-direction: column; }
      .result-head .meta { text-align: left; }
    }
    @media (max-width: 520px) {
      .metrics { grid-template-columns: 1fr; }
      .core-grid { grid-template-columns: 1fr; }
      .row { flex-direction: column; align-items: stretch; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>燃气调压器健康诊断</h1>
      <a class="nav-link" href="/rules" target="_blank">判定规则说明</a>
    </div>
  </header>
  <main>
    <section>
      <h2>输入数据</h2>
      <label for="fileSearch">搜索历史文件</label>
      <input id="fileSearch" type="text" placeholder="输入站点名或文件名" />
      <label for="fileSelect">选择历史文件</label>
      <select id="fileSelect" size="12"></select>
      <div class="row" style="margin-top:10px">
        <button id="diagnoseSelected">诊断所选文件</button>
        <button id="refreshFiles" class="secondary">刷新</button>
      </div>

      <label for="uploadInput">导入 CSV/XLSX</label>
      <input id="uploadInput" type="file" accept=".csv,.xlsx,.xls" />
      <div class="format-hint">
        <strong>上传文件格式要求</strong>
        <ul>
          <li>支持 .csv、.xlsx、.xls；Excel 默认读取第一个工作表。</li>
          <li>至少包含一列时间和一列压力值，建议样本不少于 100 行。</li>
          <li>时间列可命名为：时间、采集时间、记录时间、上报时间、record_time、timestamp；也支持“日期 + 时分秒”两列。</li>
          <li>压力列可命名为：压力、出口压力、低压出口KPa、value_kpa、pressure、measurement，单位建议为 KPa。</li>
          <li>如需正式判定阀座密封性能，可增加泄漏量列，列名包含“泄漏量、阀座泄漏、leakage”等。</li>
        </ul>
      </div>
      <button id="diagnoseUpload" style="margin-top:10px;width:100%">导入并诊断</button>
      <h2 style="margin-top:22px">判定参数</h2>
      <div class="param-grid">
        <div>
          <label for="setPressure">出口压力设定值 KPa</label>
          <input id="setPressure" type="number" step="0.001" value="2.5" />
        </div>
        <div>
          <label for="acKpa">稳压精度等级 AC %</label>
          <input id="acKpa" type="number" step="0.1" value="10" />
        </div>
        <div>
          <label for="sgKpa">关闭压力等级 SG %</label>
          <input id="sgKpa" type="number" step="0.1" value="20" />
        </div>
        <div>
          <label for="seatLeakLimit">泄漏量限值</label>
          <input id="seatLeakLimit" type="number" step="0.001" value="0.2" />
        </div>
      </div>
      <div class="param-note">三项性能评价按技术要求计算；如现场提供设备设定压力、AC、SG 或泄漏限值，应优先填写现场参数。</div>
      <div id="status" class="status"></div>

      <h2 style="margin-top:22px">诊断汇总</h2>
      <table id="summaryTable"></table>
      <div id="summaryNote" class="status"></div>
    </section>

    <section>
      <h2>核心性能诊断结果</h2>
      <div id="result" class="placeholder">请选择历史文件或上传新文件后开始诊断。</div>
    </section>
  </main>
  <script>
    let allFiles = [];

    const $ = (id) => document.getElementById(id);

    function setStatus(text) {
      $("status").textContent = text || "";
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      return res.json();
    }

    function renderFileOptions() {
      const q = $("fileSearch").value.trim().toLowerCase();
      const select = $("fileSelect");
      select.innerHTML = "";
      allFiles
        .filter(f => !q || f.toLowerCase().includes(q))
        .slice(0, 500)
        .forEach(f => {
          const opt = document.createElement("option");
          opt.value = f;
          opt.textContent = f;
          select.appendChild(opt);
        });
    }

    async function loadFiles() {
      setStatus("正在加载历史文件...");
      const data = await api("/api/files");
      allFiles = data.files;
      renderFileOptions();
      setStatus(`已加载 ${allFiles.length} 个可诊断文件`);
    }

    async function loadSummary() {
      const data = await api("/api/summary");
      const table = $("summaryTable");
      if (!data.health_distribution.length) {
        table.innerHTML = "<tr><td>暂无汇总</td></tr>";
        $("summaryNote").textContent = "";
        return;
      }
      table.innerHTML = "<tr><th>等级</th><th>状态</th><th>文件数</th></tr>" +
        data.health_distribution.map(r => `<tr><td>${r.health_level}</td><td>${r.health_label}</td><td>${r.count}</td></tr>`).join("");
      const totals = data.summary_totals || {};
      $("summaryNote").textContent = `合计 ${totals.total || 0} 条：历史批量 ${totals.history || 0} 条，网站导入 ${totals.upload || 0} 条`;
    }

    function fmt(v, digits = 3) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
      return Number(v).toFixed(digits);
    }

    function performanceParams() {
      return {
        set_pressure_kpa: $("setPressure").value,
        ac_percent: $("acKpa").value,
        sg_percent: $("sgKpa").value,
        seat_leak_limit: $("seatLeakLimit").value,
        seat_leak_surrogate_limit_kpa: $("seatLeakLimit").value
      };
    }

    function renderResult(data) {
      const profile = data.input_profile || {};
      const f = data.features;
      const h = data.health;
      const performance = data.performance || {};
      const perfParams = performance.params || {};
      const perfOverall = performance.overall || {};
      const findings = data.findings || [];
      const ai = data.ai || {};
      const links = data.report_links || {};
      const isolation = ai.isolation_forest || {};
      const knn = ai.knn || {};
      const evidence = h.evidence || {};
      const decisionReasons = (h.decision_reasons || []).map(item => `<tr><td>${item}</td></tr>`).join("") || "<tr><td>暂无判定依据</td></tr>";
      const rows = findings.length
        ? findings.map(item => `<tr><td>${item.code}</td><td>${item.name}</td><td>${item.evidence}</td><td>${item.suspected_cause}</td><td>${item.maintenance}</td></tr>`).join("")
        : "<tr><td colspan='5'>未触发故障规则</td></tr>";
      const topDeviation = (ai.top_features || []).map(item =>
        `<tr><td>${item.feature}</td><td>${item.value}</td><td>${item.baseline}</td><td>${item.z}</td></tr>`
      ).join("") || "<tr><td colspan='4'>无基线偏离特征</td></tr>";
      const performanceRows = (performance.items || []).map(item => `
        <tr>
          <td>${item.name}</td>
          <td>${fmt(item.value, 4)} ${item.unit || ""}</td>
          <td>${item.reference || ""}</td>
          <td>${fmt(item.ratio, 4)}</td>
          <td>${item.level}级 ${item.label}</td>
          <td>${item.basis || ""}</td>
        </tr>
      `).join("") || "<tr><td colspan='6'>无单项性能评价</td></tr>";
      const performanceCards = (performance.items || []).map(item => `
        <div class="metric perf-card level-${item.level || 0}">
          <div class="label">${item.name || ""}</div>
          <div class="value">${item.level || ""}级 ${item.label || ""}</div>
          <div class="subvalue">计算值 ${fmt(item.value, 4)} ${item.unit || ""} / 比值 ${fmt(item.ratio, 4)}</div>
        </div>
      `).join("");
      const blocks = (profile.blocks || []).map(item => `
        <tr>
          <td>${item.block}</td>
          <td>${item.station}</td>
          <td>${item.timestamp_source || ""}</td>
          <td>${item.pressure_column}</td>
          <td>${item.valid_rows}</td>
          <td>${item.start} 至 ${item.end}</td>
          <td>${fmt(item.min_pressure)} / ${fmt(item.max_pressure)}</td>
        </tr>
      `).join("") || "<tr><td colspan='7'>无导入识别信息</td></tr>";
      const warnings = (profile.warnings || []).length
        ? `<div class="warnings">${profile.warnings.map(item => `数据提醒：${item}`).join("<br>")}</div>`
        : "";

      $("result").className = "";
      $("result").innerHTML = `
        <div class="result-head">
          <div>
            <div class="eyebrow">核心性能诊断</div>
            <div class="title">${h.level}级 ${h.label}</div>
          </div>
          <div class="meta">${f.station || ""}<br>${f.start || ""} 至 ${f.end || ""}</div>
        </div>
        <div class="core-grid">
          <div class="metric primary">
            <div class="label">三项性能综合</div>
            <div class="value">${perfOverall.level || ""}级 ${perfOverall.label || ""}</div>
            <div class="subvalue">${perfOverall.basis || ""}</div>
          </div>
          ${performanceCards}
        </div>
        <div class="section-title">三项性能明细</div>
        <table>
          <tr><th>出口压力设定值</th><td>${fmt(perfParams.set_pressure_kpa, 4)} KPa</td><th>AC / SG</th><td>${fmt(perfParams.ac_percent, 2)}% / ${fmt(perfParams.sg_percent, 2)}%</td></tr>
          <tr><th>AC / SG换算限值</th><td>${fmt(perfParams.ac_limit_kpa, 4)} / ${fmt(perfParams.sg_limit_kpa, 4)} KPa</td><th>阀座泄漏量限值</th><td>${fmt(perfParams.seat_leak_limit, 4)} ${perfParams.seat_leak_unit || ""}</td></tr>
        </table>
        <table style="margin-top:10px">
          <tr><th>项目</th><th>计算值</th><th>参照值</th><th>比值</th><th>单项等级</th><th>计算依据</th></tr>
          ${performanceRows}
        </table>
        ${performance.note ? `<div class="param-note">${performance.note}</div>` : ""}
        <div class="section-title">压力运行支持数据</div>
        <table>
          <tr><th>站点</th><td>${f.station}</td><th>样本数</th><td>${f.count}</td></tr>
          <tr><th>时间范围</th><td colspan="3">${f.start} 至 ${f.end}</td></tr>
          <tr><th>平均压力</th><td>${fmt(f.mean)} KPa</td><th>最小/最大</th><td>${fmt(f.min)} / ${fmt(f.max)} KPa</td></tr>
          <tr><th>超过2.75</th><td>${(f.high_275_ratio * 100).toFixed(1)}%</td><th>超过3.0</th><td>${(f.high_300_ratio * 100).toFixed(1)}%</td></tr>
        </table>
        <div class="aux-block">
          <div class="section-title" style="margin-top:0">辅助参考</div>
          <p class="aux-note">以下规则、IF/KNN和基线结果用于解释数据偏离情况，最终报告仍以三项核心性能评价为主。</p>
          <div class="metrics">
            <div class="metric"><div class="label">综合风险分</div><div class="value">${fmt(h.risk_score, 4)}</div></div>
            <div class="metric"><div class="label">异常分</div><div class="value">${fmt(h.ai_score, 4)}</div></div>
            <div class="metric"><div class="label">Isolation Forest异常分</div><div class="value">${fmt(isolation.score, 4)}</div></div>
            <div class="metric"><div class="label">KNN异常分</div><div class="value">${fmt(knn.score, 4)}</div></div>
          </div>
          <table>
            <tr><th>辅助证据等级</th><td colspan="3">规则${evidence.rule_level || ""} / IF${evidence.isolation_level || ""} / KNN${evidence.knn_level || ""} / 基线${evidence.baseline_level || ""} / 趋势${evidence.trend_level || ""}</td></tr>
            <tr><th>IF异常区间</th><td>${isolation.band_label || ""}${isolation.percentile !== undefined ? ` / P${isolation.percentile}` : ""}</td><th>KNN异常区间</th><td>${knn.band_label || ""}${knn.percentile !== undefined ? ` / P${knn.percentile}` : ""}</td></tr>
          </table>
          <h3>判定依据</h3>
          <table><tr><th>依据说明</th></tr>${decisionReasons}</table>
          <h3>规则触发</h3>
          <table><tr><th>规则</th><th>异常类型</th><th>证据</th><th>疑似原因</th><th>建议</th></tr>${rows}</table>
          <h3>基线偏离</h3>
          <table><tr><th>特征</th><th>当前值</th><th>健康基线</th><th>偏离Z</th></tr>${topDeviation}</table>
        </div>
        <div class="quality">
          <h3>导入识别</h3>
          <table>
            <tr><th>文件</th><td>${profile.file_name || ""}</td><th>类型</th><td>${profile.file_type || ""}</td></tr>
            <tr><th>原始行/列</th><td>${profile.raw_rows || 0} / ${profile.raw_columns || 0}</td><th>有效/无效样本</th><td>${profile.valid_rows || 0} / ${profile.invalid_rows || 0}</td></tr>
          </table>
          <table style="margin-top:10px">
            <tr><th>块</th><th>站点</th><th>时间来源</th><th>压力列</th><th>有效样本</th><th>时间范围</th><th>最小/最大 KPa</th></tr>
            ${blocks}
          </table>
          ${warnings}
        </div>
        <div class="links" style="margin-top:16px">
          ${(links.overview_pdf || links.pdf) ? `<a href="${links.overview_pdf || links.pdf}" download>导出PDF报告</a>` : ""}
        </div>
      `;
    }

    async function diagnosePath(path) {
      setStatus("正在诊断...");
      $("diagnoseSelected").disabled = true;
      try {
        const data = await api("/api/diagnose", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, performance_params: performanceParams() })
        });
        renderResult(data);
        setStatus("诊断完成");
      } finally {
        $("diagnoseSelected").disabled = false;
      }
    }

    async function diagnoseUpload() {
      const file = $("uploadInput").files[0];
      if (!file) {
        setStatus("请先选择文件");
        return;
      }
      setStatus("正在导入并诊断...");
      $("diagnoseUpload").disabled = true;
      try {
        const buf = await file.arrayBuffer();
        const data = await api("/api/upload", {
          method: "POST",
          headers: {
            "X-Filename": encodeURIComponent(file.name),
            "X-Performance-Params": encodeURIComponent(JSON.stringify(performanceParams()))
          },
          body: buf
        });
        renderResult(data);
        setStatus("导入诊断完成");
        loadSummary().catch(() => {});
      } finally {
        $("diagnoseUpload").disabled = false;
      }
    }

    $("fileSearch").addEventListener("input", renderFileOptions);
    $("refreshFiles").addEventListener("click", loadFiles);
    $("diagnoseSelected").addEventListener("click", () => {
      const value = $("fileSelect").value;
      if (!value) return setStatus("请选择一个历史文件");
      diagnosePath(value).catch(err => setStatus("诊断失败：" + err.message));
    });
    $("diagnoseUpload").addEventListener("click", () => diagnoseUpload().catch(err => setStatus("导入诊断失败：" + err.message)));

    loadFiles().catch(err => setStatus("加载文件失败：" + err.message));
    loadSummary().catch(() => {});
  </script>
</body>
</html>
"""


if STATIC_INDEX.exists():
    HTML = STATIC_INDEX.read_text(encoding="utf-8")


RULES_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>判定规则说明 - 燃气调压器健康诊断系统</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {
      color-scheme: light;
      --bg: #F0F2F8;
      --surface: #FFFFFF;
      --ink: #111827;
      --ink-2: #374151;
      --muted: #6B7280;
      --soft: #9CA3AF;
      --line: #E5E7EB;
      --line-soft: #F3F4F6;
      --primary: #1A73E8;
      --primary-light: #EEF4FF;
      --font: "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      --font-mono: "SF Mono", "Cascadia Code", "Consolas", "Monaco", monospace;
      --radius: 12px;
      --radius-lg: 16px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font);
      background: var(--bg);
      color: var(--ink);
      line-height: 1.68;
      -webkit-font-smoothing: antialiased;
    }
    .topnav {
      height: 64px; background: var(--surface);
      border-bottom: 1px solid var(--line);
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 24px; position: sticky; top: 0; z-index: 100;
    }
    .nav-left { display: flex; align-items: center; gap: 14px; }
    .logo {
      width: 34px; height: 34px; border-radius: 10px;
      background: var(--primary); display: grid; place-items: center;
      color: #fff; font-weight: 700; font-size: 16px;
    }
    .app-title { font-size: 18px; font-weight: 600; color: var(--ink); letter-spacing: -0.01em; }
    .nav-link {
      font-size: 13px; font-weight: 500; color: var(--primary);
      padding: 8px 16px; border-radius: 999px;
      background: var(--primary-light); text-decoration: none;
      transition: background .15s;
    }
    .nav-link:hover { background: #dce8fc; }
    main {
      max-width: 1120px; margin: 0 auto; padding: 32px 24px;
    }
    section {
      background: var(--surface); border: 1px solid var(--line);
      border-radius: var(--radius-lg); padding: 28px; margin-bottom: 20px;
    }
    h2 { margin: 0 0 16px; font-size: 18px; font-weight: 600; color: var(--ink); }
    h3 { margin: 22px 0 10px; font-size: 15px; font-weight: 600; color: var(--ink-2); }
    p { margin: 8px 0; color: var(--ink-2); font-size: 14px; }
    ul { margin: 8px 0 0; padding-left: 20px; color: var(--ink-2); font-size: 14px; }
    li { margin: 4px 0; }
    table {
      width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px;
    }
    th, td {
      border-bottom: 1px solid var(--line-soft); text-align: left;
      padding: 10px 12px; vertical-align: top;
    }
    th {
      color: var(--soft); font-weight: 600; font-size: 12px;
      background: var(--line-soft);
    }
    tr:last-child th, tr:last-child td { border-bottom: 0; }
    code {
      background: var(--primary-light); border: 1px solid var(--line);
      border-radius: 6px; padding: 2px 6px;
      font-family: var(--font-mono); font-size: 12px; color: var(--primary);
    }
    .formula {
      background: var(--line-soft); border: 1px solid var(--line);
      border-radius: var(--radius); padding: 16px;
      color: var(--ink); overflow-x: auto; white-space: nowrap;
      font-family: var(--font-mono); font-size: 13px;
    }
    .pill {
      display: inline-block; padding: 2px 10px; border-radius: 999px;
      font-size: 12px; font-weight: 600; background: var(--primary-light); color: var(--primary);
    }
    @media (max-width: 720px) {
      .topnav { padding: 0 16px; }
      main { padding: 20px 16px; }
      section { padding: 20px; }
    }
  </style>
</head>
<body>
  <nav class="topnav">
    <div class="nav-left">
      <div class="logo">G</div>
      <span class="app-title">燃气调压器健康诊断系统</span>
    </div>
    <a class="nav-link" href="/">← 返回诊断页面</a>
  </nav>
  <main>
    <section>
      <h2>一、总体判定流程</h2>
      <p>系统采用“三项核心性能评价为主，压力规则、健康基线、Isolation Forest、KNN 和趋势为辅”的组合校准诊断方式。三项核心性能指标直接参与综合风险分计算，并作为最终健康等级的重要依据；IF/KNN和基线用于补充识别历史数据中的异常偏离。</p>
      <table>
        <tr><th>步骤</th><th>作用</th><th>输出</th></tr>
        <tr><td>1. 数据识别</td><td>识别时间列、压力列、站点名，清洗有效压力样本。</td><td>有效样本、时间范围、压力列、站点</td></tr>
        <tr><td>2. 特征提取</td><td>计算压力统计、超限比例、夜间压力、波动次数、趋势斜率等。</td><td>诊断特征向量</td></tr>
        <tr><td>3. 单项性能评价</td><td>按技术要求计算稳压性能、关闭压力性能、阀座密封性能。</td><td>三项单项等级、单项综合等级</td></tr>
        <tr><td>4. 规则判断</td><td>按技术要求判断超压、低压、持续升压、采集异常等明确风险。</td><td>规则风险等级</td></tr>
        <tr><td>5. IF/KNN异常检测</td><td>Isolation Forest 和 KNN 判断样本是否偏离历史健康数据。</td><td>IF/KNN异常分、异常区间</td></tr>
        <tr><td>6. 组合判级</td><td>以三项性能评分为主，结合规则、模型、基线和趋势，输出 1-5 级健康等级。</td><td>健康等级、综合风险分、判定依据</td></tr>
      </table>
    </section>

    <section>
      <h2>二、三项核心性能评价</h2>
      <p>三项核心性能指标作为系统的核心判定层。AC 为稳压精度等级，SG 为关闭压力等级，通常按出口压力设定值的百分比换算为 KPa 限值；阀座密封性能优先使用导入数据中的泄漏量字段，只有压力采样时仅作为估算。</p>
      <table>
        <tr><th>项目</th><th>系统计算方式</th><th>参照值</th><th>分级规则</th></tr>
        <tr><td>调压器稳压性能</td><td>取日间最高N点均值 Pmax、日间最低N点均值 Pmin，计算实际AC = max(Pmax - 设定压力, 设定压力 - Pmin, 0) / 设定压力 × 100%。</td><td>出厂AC%</td><td>实际AC/出厂AC ≤0.8 为优良；≤1 为合格；≤1.25 为轻度偏差；≤1.5 为较大偏差；超过 1.5 为严重偏差。</td></tr>
        <tr><td>调压器关闭压力性能</td><td>用实测关闭压力和实测运行压力设定值计算实际SG = max(关闭压力 - 设定压力, 0) / 设定压力 × 100%；没有关闭压力字段时，低流量锁闭投影仅作为估算。</td><td>出厂SG%</td><td>实际SG/出厂SG ≤0.8 为优良；≤1 为合格；≤1.5 为轻度偏差；≤2 为较大偏差；超过 2 为严重偏差。</td></tr>
        <tr><td>调压器阀座密封性能</td><td>有泄漏量字段时按泄漏量判定；无泄漏量字段时计算低流量/近关闭片段的 P90 正向爬升、1小时升压斜率折算值和爬升窗口复现比例，形成密封爬升指数。</td><td>正式字段使用泄漏量限值；压力侧估算使用动态参考量，并结合 AC 允许偏差收紧。</td><td>正式泄漏量：≤0.8倍优良，≤1倍合格，≤5倍轻度偏差，≤10倍较大偏差，>10倍严重偏差。压力估算：指数≤80%优良，≤120%合格，≤220%轻度偏差，≤350%较大偏差，>350%严重偏差。</td></tr>
      </table>
      <h3>三项综合等级</h3>
      <table>
        <tr><th>综合等级</th><th>判定条件</th></tr>
        <tr><td>1级 优良</td><td>三项单项检测结果全部为优良。</td></tr>
        <tr><td>2级 健康</td><td>存在一项或多项合格项目，且没有轻度偏差及以上项目。</td></tr>
        <tr><td>3级 亚健康</td><td>轻度偏差项目不超过 2 项，且无较大偏差或严重偏差。</td></tr>
        <tr><td>4级 风险</td><td>存在 1 项较大偏差，或轻度偏差项目超过 2 项。</td></tr>
        <tr><td>5级 高风险</td><td>存在 1 项严重偏差，或存在 2 项及以上较大偏差。</td></tr>
      </table>
      <p>页面中的出口压力设定值、AC、SG、阀座泄漏量限值可以按现场参数填写；未填写时使用系统默认参数进行计算。由于当前数据主要是压力采样，阀座密封性能在没有泄漏量字段时不能作为正式泄漏量检测结论，只能作为压力采样估算。</p>
    </section>

    <section>
      <h2>三、压力规则层</h2>
      <p>规则层用于识别明确的安全或运行风险。规则触发后会作为健康等级的安全底线。</p>
      <table>
        <tr><th>规则</th><th>触发条件</th><th>含义</th><th>严重度</th></tr>
        <tr><td>R01 压力快速偏高 / 放散风险</td><td>最大压力 ≥ 3.0 KPa，且超 2.75/3.0 KPa 比例或连续超 3.0 分钟数达到条件。</td><td>可能存在超压、放散风险。</td><td>4-5</td></tr>
        <tr><td>R02 运行压力偏高</td><td>平均压力 ≥ 2.75 KPa，或超过 3.0 KPa 比例 ≥ 5%。</td><td>整体运行压力偏高。</td><td>4</td></tr>
        <tr><td>R03 高峰期压力偏低</td><td>低于 2.0 KPa 比例 ≥ 5%，或高峰期低压比例 ≥ 10%。</td><td>高峰用气时供压不足。</td><td>4</td></tr>
        <tr><td>R04 低峰期关闭不严 / 持续升压</td><td>夜间最大压力 ≥ 3.0 KPa，或夜间超过 3.0 KPa 比例 ≥ 5%。</td><td>低峰时可能关闭不严或持续升压。</td><td>4-5</td></tr>
        <tr><td>R05 关闭压力偏高</td><td>夜间最大压力 ≥ 3.1 KPa，且超过 3.1 KPa 比例 ≥ 1%。</td><td>关闭压力偏高。</td><td>4</td></tr>
        <tr><td>R06 波动频率异常</td><td>波动次数 ≥ 6，且典型波动间隔 ≤ 20 分钟。</td><td>可能存在卡顿、皮膜老化等波动问题。</td><td>3</td></tr>
        <tr><td>R07 长期缓慢下降趋势</td><td>压力趋势斜率 ≤ -0.01 KPa/日。</td><td>可能存在长期漂移或弹簧状态变化。</td><td>3</td></tr>
        <tr><td>R08 采集异常或极端压力值</td><td>最小压力 ≤ 0.05 KPa，或最大压力 ≥ 6.0 KPa。</td><td>可能是传感器、采集链路或极端工况异常。</td><td>4</td></tr>
      </table>
    </section>

    <section>
      <h2>四、健康基线偏离</h2>
      <p>系统会从历史数据中筛选相对健康的运行窗口，建立健康基线。新数据导入后提取同样的特征，并与健康基线比较。</p>
      <h3>主要特征</h3>
      <p><code>mean</code> 平均压力、<code>std</code> 标准差、<code>min/max</code> 最小/最大压力、<code>high_275_ratio</code> 超 2.75 比例、<code>high_300_ratio</code> 超 3.0 比例、<code>low_200_ratio</code> 低于 2.0 比例、<code>night_max</code> 夜间最大压力、<code>wave_count</code> 波动次数、<code>slope_per_day</code> 趋势斜率等。</p>
      <h3>偏离分</h3>
      <p>每个特征会计算相对健康基线的偏离程度，类似 Z 值。偏离越大，说明当前样本相对健康基准的差异越明显。</p>
      <table>
        <tr><th>基线证据等级</th><th>条件</th><th>解释</th></tr>
        <tr><td>1</td><td>偏离较小</td><td>接近健康基线。</td></tr>
        <tr><td>2</td><td>基线偏离分 ≥ 0.30，或最大特征偏离 Z ≥ 2.0</td><td>轻微偏离。</td></tr>
        <tr><td>3</td><td>基线偏离分 ≥ 0.55，或最大特征偏离 Z ≥ 3.5</td><td>明显偏离。</td></tr>
        <tr><td>4</td><td>基线偏离分 ≥ 0.75，或最大特征偏离 Z ≥ 5.0</td><td>强偏离。</td></tr>
      </table>
    </section>

    <section>
      <h2>五、Isolation Forest 异常检测</h2>
      <p>Isolation Forest 用于识别多维运行特征中的离群样本。系统以历史健康窗口提取的特征向量作为训练基准，特征包括压力均值、标准差、最大/最小值、超限比例、夜间压力、波动次数和趋势斜率等。</p>
      <p>新数据导入后，系统提取同样的特征向量，并计算其 Isolation Forest 原始异常得分。该得分不会直接作为结论，而是与健康训练样本的得分分布进行分位校准。例如，结果显示 <code>P95.4</code>，表示当前样本的异常程度高于约 95.4% 的健康基准样本。</p>
      <table>
        <tr><th>分位范围</th><th>区间解释</th><th>模型分参考</th></tr>
        <tr><td>P &lt; 90</td><td>正常范围</td><td>低于约 0.30</td></tr>
        <tr><td>P90 - P95</td><td>轻微异常</td><td>约 0.30 - 0.60</td></tr>
        <tr><td>P95 - P99</td><td>明显异常</td><td>约 0.60 - 0.85</td></tr>
        <tr><td>P99 以上</td><td>强异常</td><td>约 0.85 - 1.00</td></tr>
      </table>
    </section>

    <section>
      <h2>六、KNN 近邻异常检测</h2>
      <p>KNN 近邻检测用于衡量当前样本与历史健康样本之间的相似程度。系统在统一特征空间中寻找最近的 5 个健康样本，并计算平均近邻距离，作为样本偏离健康基准的距离指标。</p>
      <p>为避免直接使用绝对距离造成误判，系统同样采用健康样本自身的近邻距离分布进行分位校准。例如，KNN 结果为 <code>P95.4</code> 时，表示当前样本的近邻距离高于约 95.4% 的健康基准样本，说明该样本与常见健康运行状态存在明显距离。</p>
      <p>在最终判级时，KNN 不单独决定健康等级，而是与 Isolation Forest、健康基线偏离和规则触发结果共同形成证据链。</p>
    </section>

    <section>
      <h2>七、异常分与综合风险分</h2>
      <h3>异常分</h3>
      <div class="formula">异常分 = 0.40 × 健康基线偏离分 + 0.30 × Isolation Forest异常分 + 0.30 × KNN异常分</div>
      <p>异常分主要反映数据驱动的异常程度，不直接等同于安全风险。</p>
      <h3>三项性能风险分</h3>
      <div class="formula">三项性能风险分 = max(三项综合等级对应底线分, 0.70 × 三项单项最高风险分 + 0.30 × 三项单项平均风险分)</div>
      <p>三项性能风险分由稳压性能、关闭压力性能、阀座密封性能直接计算，是综合风险分的主项。</p>
      <h3>辅助证据分</h3>
      <div class="formula">辅助证据分 = max(0.46 × 规则风险分 + 0.18 × 基线偏离分 + 0.16 × IF异常分 + 0.16 × KNN异常分 + 0.04 × 趋势分, 0.52 × 异常分)</div>
      <p>辅助证据分用于补充识别安全规则触发、历史基线偏离和模型异常。</p>
      <h3>综合风险分</h3>
      <div class="formula">综合风险分 = 0.85 × 三项性能风险分 + 0.15 × 辅助证据分</div>
      <p>综合风险分以三项核心性能为主，同时保留规则、IF/KNN和基线作为辅助校核。如果最终健康等级较高，系统还会设置最低风险分，避免等级和分数不一致。</p>
      <table>
        <tr><th>健康等级</th><th>最低综合风险分</th></tr>
        <tr><td>1级 优良</td><td>0.00</td></tr>
        <tr><td>2级 健康</td><td>0.18</td></tr>
        <tr><td>3级 亚健康</td><td>0.35</td></tr>
        <tr><td>4级 风险</td><td>0.60</td></tr>
        <tr><td>5级 高风险</td><td>0.80</td></tr>
      </table>
    </section>

    <section>
      <h2>八、最终健康等级</h2>
      <p>最终等级采用核心性能优先的证据一致性判断。三项核心性能评价先形成单项综合等级，并作为最终等级的重要下限；明确安全规则作为安全底线；没有硬规则时，Isolation Forest、KNN 和健康基线需要形成多类异常证据，才会提高等级。</p>
      <table>
        <tr><th>等级</th><th>名称</th><th>典型条件</th></tr>
        <tr><td>1级</td><td><span class="pill">优良</span></td><td>无规则触发，IF/KNN和基线均接近健康范围。</td></tr>
        <tr><td>2级</td><td><span class="pill">健康</span></td><td>有轻微偏离，但证据不足以构成亚健康。</td></tr>
        <tr><td>3级</td><td><span class="pill">亚健康</span></td><td>至少两类证据提示异常，但未达到明确安全风险。</td></tr>
        <tr><td>4级</td><td><span class="pill">风险</span></td><td>三项性能综合为4级，或触发 4 级规则，或 IF、KNN、基线形成强一致异常证据。</td></tr>
        <tr><td>5级</td><td><span class="pill">高风险</span></td><td>三项性能综合为5级，或触发 5 级安全规则，或 4 级规则叠加强异常证据。</td></tr>
      </table>
    </section>
  </main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "GasDiagnosisWeb/0.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                if STATIC_INDEX.exists():
                    self._send(200, STATIC_INDEX.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/rules":
                if STATIC_RULES.exists():
                    self._send(200, STATIC_RULES.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, RULES_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/files":
                files = [
                    str(p.relative_to(ROOT)).replace("\\", "/")
                    for p in discover_files(ROOT, [".csv", ".xlsx", ".xls"])
                ]
                self._send_json({"files": files})
            elif parsed.path == "/api/summary":
                self._send_json(_summary_payload())
            elif parsed.path == "/file":
                qs = parse_qs(parsed.query)
                target = _safe_report_path(qs.get("path", [""])[0])
                ctype = "text/plain; charset=utf-8"
                if target.suffix.lower() == ".html":
                    ctype = "text/html; charset=utf-8"
                elif target.suffix.lower() == ".json":
                    ctype = "application/json; charset=utf-8"
                elif target.suffix.lower() == ".pdf":
                    ctype = "application/pdf"
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._send_error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if parsed.path == "/api/diagnose":
                payload = json.loads(body.decode("utf-8"))
                target = _safe_resolve(str(payload.get("path", "")))
                self._send_json(_diagnose_path(target, _performance_params_from_payload(payload)))
            elif parsed.path == "/api/llm_analysis":
                payload = json.loads(body.decode("utf-8") or "{}")
                result = payload.get("result") or payload
                self._send_json(_deepseek_analysis(_compact_llm_result(result)))
            elif parsed.path == "/api/reports_zip":
                payload = json.loads(body.decode("utf-8") or "{}")
                reports = payload.get("reports") or []
                if not reports:
                    self._send_error(400, "no reports to package")
                    return
                buffer = io.BytesIO()
                used_names: set[str] = set()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for index, item in enumerate(reports, 1):
                        if isinstance(item, str):
                            path_value = item
                            display_name = Path(item).stem
                        else:
                            path_value = item.get("path", "")
                            display_name = item.get("name") or Path(path_value).stem
                        target = _report_path_from_link(path_value)
                        arcname = _safe_zip_name(display_name, index)
                        while arcname in used_names:
                            arcname = _safe_zip_name(f"{display_name}_{index}", index)
                        used_names.add(arcname)
                        zf.write(target, arcname)
                self._send_download(buffer.getvalue(), "application/zip", "diagnosis_pdf_reports.zip")
            elif parsed.path == "/api/upload":
                name = unquote(self.headers.get("X-Filename", "upload.csv"))
                raw_params = unquote(self.headers.get("X-Performance-Params", "{}"))
                performance_params = _performance_params_from_payload(json.loads(raw_params or "{}"))
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                target = UPLOAD_DIR / _safe_upload_filename(name)
                target.write_bytes(body)
                result = _diagnose_path(target, performance_params)
                _append_upload_log(name, target, result)
                result["logged"] = True
                self._send_json(result)
            else:
                self._send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._send_error(500, str(exc))

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("GAS_QUIET") == "1":
            return
        print(f"[web] {self.address_string()} - {fmt % args}")


def serve(host: str = "127.0.0.1", port: int = 8765, quiet: bool = False) -> None:
    """启动 HTTP 服务（桌面应用入口）。"""
    if quiet:
        os.environ["GAS_QUIET"] = "1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    if not quiet:
        print(f"Gas diagnosis web app running at http://{host}:{port}")
    httpd.serve_forever()


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动 HTTP 服务（开发环境入口，等同于 serve）。"""
    serve(host, port)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="启动燃气调压器诊断Web工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
