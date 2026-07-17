"""Generate formal diagnosis reports."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import html
import json

import pandas as pd

from .pdf_report import render_html_to_pdf


def _pct(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{float(value) * 100:.1f}%"


def _num(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _level_summary(level: int, label: str, findings: list[dict]) -> str:
    if level >= 5:
        return "设备运行数据存在高风险特征，建议立即组织现场复核和处置。"
    if level == 4:
        return "设备运行数据存在明显风险特征，建议尽快安排专项检查或维保。"
    if level == 3:
        return "设备运行数据存在轻微异常或趋势偏离，建议缩短复检周期并持续观察。"
    if level == 2:
        return "设备运行数据总体稳定，未发现需要立即处置的风险特征。"
    return "设备运行数据表现良好，当前窗口内未见明显异常。"


def _maintenance_summary(health: dict, findings: list[dict]) -> str:
    if findings:
        priorities = [item.get("maintenance", "") for item in findings if item.get("maintenance")]
        if priorities:
            unique = list(dict.fromkeys(priorities))
            urgency_order = ["立即处理", "立即维保", "立即", "1个月内维保", "一个月以内维保", "6个月内维保"]
            for urgent in urgency_order:
                for item in unique:
                    if urgent in item:
                        if len(unique) > 1:
                            return f"{item}；其余异常按明细建议同步关注"
                        return item
            return "；".join(unique)
    level = int(health["level"])
    if level >= 4:
        return "建议先开展现场检查或维修，复检合格后再确定后续诊断周期。"
    if level == 3:
        return "建议半年内复检，并持续关注压力趋势和波动变化。"
    if level == 2:
        return "建议按年度周期进行常规健康诊断。"
    return "可按年度周期或适当延长周期进行健康诊断，最长不超过15个月。"


def _format_tick_label(ts: pd.Timestamp, span_seconds: float) -> str:
    if pd.isna(ts):
        return ""
    if span_seconds >= 24 * 3600:
        return ts.strftime("%m-%d %H:%M")
    return ts.strftime("%H:%M")


def _legacy_sparkline_svg(data: pd.DataFrame, width: int = 980, height: int = 300) -> str:
    if data.empty:
        return "<svg></svg>"
    df = data.sort_values("timestamp")
    y = df["pressure_kpa"].astype(float)
    min_y = min(float(y.min()), 1.8)
    max_y = max(float(y.max()), 3.2)
    if abs(max_y - min_y) < 1e-9:
        max_y += 0.5
        min_y -= 0.5

    left, right, top, bottom = 48, 70, 28, 48
    plot_w = width - left - right
    plot_h = height - top - bottom

    def y_pos(v: float) -> float:
        return top + (max_y - v) * plot_h / (max_y - min_y)

    points = []
    for i, value in enumerate(y):
        x = left + i * plot_w / max(1, len(y) - 1)
        yy = y_pos(float(value))
        points.append(f"{x:.1f},{yy:.1f}")

    grid = []
    for level in [2.0, 2.5, 3.0]:
        if min_y <= level <= max_y:
            yy = y_pos(level)
            grid.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
            grid.append(f'<text x="8" y="{yy+4:.1f}" font-size="11" fill="#6b7280">{level:.1f}</text>')

    refs = []
    for level, color, label in [
        (2.0, "#dc2626", "2.0低压参考"),
        (2.75, "#d97706", "2.75预警参考"),
        (3.0, "#b91c1c", "3.0高压参考"),
        (3.1, "#7f1d1d", "3.1放散参考"),
    ]:
        if min_y <= level <= max_y:
            yy = y_pos(level)
            refs.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="{color}" stroke-width="1.2" stroke-dasharray="6 4"/>')
            refs.append(f'<text x="{width-right+6}" y="{yy+4:.1f}" font-size="11" fill="{color}">{label}</text>')

    start_ts = pd.to_datetime(df["timestamp"].min())
    end_ts = pd.to_datetime(df["timestamp"].max())
    span_seconds = max(0.0, (end_ts - start_ts).total_seconds()) if pd.notna(start_ts) and pd.notna(end_ts) else 0.0
    ticks = []
    tick_count = min(6, max(2, len(df)))
    axis_y = height - bottom
    for idx in range(tick_count):
        ratio = idx / max(1, tick_count - 1)
        x_tick = left + ratio * plot_w
        if span_seconds > 0:
            tick_ts = start_ts + pd.to_timedelta(span_seconds * ratio, unit="s")
            tick_label = html.escape(_format_tick_label(tick_ts, span_seconds))
        else:
            tick_label = str(int(round(ratio * (len(df) - 1))) + 1)
        ticks.append(f'<line x1="{x_tick:.1f}" y1="{top}" x2="{x_tick:.1f}" y2="{axis_y}" stroke="#eef2f7" stroke-width="1"/>')
        ticks.append(f'<line x1="{x_tick:.1f}" y1="{axis_y}" x2="{x_tick:.1f}" y2="{axis_y + 5}" stroke="#9ca3af" stroke-width="1"/>')
        ticks.append(f'<text x="{x_tick:.1f}" y="{axis_y + 18}" font-size="11" fill="#6b7280" text-anchor="middle">{tick_label}</text>')
    start = html.escape(str(df["timestamp"].min()))
    end = html.escape(str(df["timestamp"].max()))
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>'
        f'<text x="{left}" y="18" font-size="14" font-weight="700" fill="#111827">压力运行曲线 KPa</text>'
        + "".join(grid)
        + f'<line x1="{left}" y1="{axis_y}" x2="{width-right}" y2="{axis_y}" stroke="#d1d5db" stroke-width="1"/>'
        + "".join(ticks)
        + "".join(refs)
        + f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{" ".join(points)}"/>'
        + f'<text x="{left}" y="{height-10}" font-size="11" fill="#6b7280">{start} 至 {end}</text>'
        + "</svg>"
    )


def _curve_thresholds(result: dict | None) -> dict:
    perf = (result or {}).get("performance") or {}
    params = perf.get("params") or {}

    def as_float(value: object, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    set_pressure = as_float(params.get("set_pressure_kpa"))
    ac_limit = as_float(params.get("ac_limit_kpa"))
    sg_limit = as_float(params.get("sg_limit_kpa"))
    if not ac_limit and set_pressure and params.get("ac_percent") is not None:
        ac_limit = abs(set_pressure) * as_float(params.get("ac_percent")) / 100.0
    if not sg_limit and set_pressure and params.get("sg_percent") is not None:
        sg_limit = abs(set_pressure) * as_float(params.get("sg_percent")) / 100.0
    return {
        "set": set_pressure or None,
        "ac": ac_limit or None,
        "sg": sg_limit or None,
        "ac_high": set_pressure + ac_limit if set_pressure and ac_limit else None,
        "ac_low": set_pressure - ac_limit if set_pressure and ac_limit else None,
        "sg_high": set_pressure + sg_limit if set_pressure and sg_limit else None,
    }


def _curve_point_state(value: float, thresholds: dict) -> str:
    if thresholds.get("sg_high") is not None and value > float(thresholds["sg_high"]):
        return "sg-high"
    if thresholds.get("ac_high") is not None and value > float(thresholds["ac_high"]):
        return "ac-high"
    if thresholds.get("ac_low") is not None and value < float(thresholds["ac_low"]):
        return "ac-low"
    return ""


def _curve_segments(values: list[float], thresholds: dict) -> tuple[list[dict], dict[str, int]]:
    segments: list[dict] = []
    counts = {"sg-high": 0, "ac-high": 0, "ac-low": 0}
    current: dict | None = None
    for idx, value in enumerate(values):
        state = _curve_point_state(value, thresholds)
        if state:
            counts[state] = counts.get(state, 0) + 1
        if state and current and current["state"] == state:
            current["end"] = idx
            current["max"] = max(float(current["max"]), value)
            current["min"] = min(float(current["min"]), value)
        else:
            if current:
                segments.append(current)
            current = {"state": state, "start": idx, "end": idx, "max": value, "min": value} if state else None
    if current:
        segments.append(current)
    return segments, counts


def _performance_item_from_result(result: dict | None, code: str) -> dict:
    performance = ((result or {}).get("performance") or {})
    for item in performance.get("items") or []:
        if item.get("code") == code:
            return item
    return {}


def _performance_evidence_value(item: dict, name: str) -> float | None:
    for metric in item.get("evidence_metrics") or []:
        if metric.get("name") == name:
            try:
                return float(metric.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _curve_key_markers(values: list[float], result: dict | None) -> list[dict]:
    p01 = _performance_item_from_result(result, "P01")
    marker_data = p01.get("curve_markers") or {}
    try:
        source_count = int(marker_data.get("source_count") or marker_data.get("total_count") or 0)
    except (TypeError, ValueError):
        source_count = 0
    markers = []
    for label, color, points in [
        ("Pmax样本", "#f59e0b", marker_data.get("pmax_points") or []),
        ("Pmin样本", "#0ea5e9", marker_data.get("pmin_points") or []),
    ]:
        for point in points:
            try:
                raw_idx = float(point.get("index"))
            except (TypeError, ValueError, AttributeError):
                raw_idx = float("nan")
            if source_count > 1 and raw_idx == raw_idx:
                idx = int(round(raw_idx * (len(values) - 1) / (source_count - 1)))
            else:
                try:
                    idx_value = point.get("curve_index")
                    if idx_value is None:
                        idx_value = point.get("index")
                    idx = int(round(float(idx_value)))
                except (TypeError, ValueError, AttributeError):
                    continue
            if idx < 0 or idx >= len(values):
                continue
            markers.append({"idx": idx, "value": values[idx], "label": label, "color": color})
    return markers


def _curve_mean_lines(result: dict | None) -> list[dict]:
    p01 = _performance_item_from_result(result, "P01")
    marker_data = p01.get("curve_markers") or {}
    lines = []
    for label, color, key in [
        ("Pmax均值", "#d97706", "pmax_mean"),
        ("Pmin均值", "#0284c7", "pmin_mean"),
    ]:
        try:
            value = float(marker_data.get(key))
        except (TypeError, ValueError):
            continue
        lines.append({"label": label, "color": color, "value": value})
    return lines


def _significant_segments(segments: list[dict], limit: int = 40) -> list[dict]:
    filtered = [
        segment for segment in segments
        if segment.get("state") == "sg-high" or int(segment.get("end", 0)) - int(segment.get("start", 0)) + 1 >= 2
    ]
    filtered.sort(
        key=lambda segment: (
            1000 if segment.get("state") == "sg-high" else 0,
            int(segment.get("end", 0)) - int(segment.get("start", 0)) + 1,
        ),
        reverse=True,
    )
    return sorted(filtered[:limit], key=lambda segment: int(segment.get("start", 0)))


def sparkline_svg(data: pd.DataFrame, result: dict | None = None, width: int = 1800, height: int = 420) -> str:
    if data.empty:
        return "<svg></svg>"
    df = data.sort_values("timestamp")
    values = pd.to_numeric(df["pressure_kpa"], errors="coerce").dropna().astype(float).tolist()
    if not values:
        return "<svg></svg>"

    thresholds = _curve_thresholds(result)
    low_flow = ((result or {}).get("performance") or {}).get("low_flow_analysis") or {}
    candidates = values[:]
    for value in [
        thresholds.get("set"),
        thresholds.get("ac_high"),
        thresholds.get("ac_low"),
        thresholds.get("sg_high"),
        low_flow.get("lockup_pressure"),
        low_flow.get("lockup_projection"),
        2.0,
        2.75,
        3.0,
    ]:
        if value is not None:
            candidates.append(float(value))
    for line in _curve_mean_lines(result):
        candidates.append(float(line["value"]))
    min_y = min(candidates)
    max_y = max(candidates)
    y_range = max(0.5, max_y - min_y)
    min_y = max(0.0, min_y - y_range * 0.12)
    max_y = max_y + y_range * 0.12

    left, right, top, bottom = 52, 96, 26, 58
    plot_w = width - left - right
    plot_h = height - top - bottom
    axis_y = height - bottom

    def x_pos(i: int) -> float:
        return left + i * plot_w / max(1, len(values) - 1)

    def y_pos(value: float) -> float:
        return top + (max_y - value) * plot_h / max(1e-9, max_y - min_y)

    points = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(values))
    segments, counts = _curve_segments(values, thresholds)

    grid_parts = []
    grid_seed = [
        min_y,
        min_y + (max_y - min_y) * 0.25,
        min_y + (max_y - min_y) * 0.5,
        min_y + (max_y - min_y) * 0.75,
        max_y,
        thresholds.get("set"),
        thresholds.get("ac_high"),
        thresholds.get("ac_low"),
        thresholds.get("sg_high"),
        low_flow.get("lockup_projection"),
    ]
    grid_values: list[float] = []
    for value in grid_seed:
        if value is None:
            continue
        numeric_value = float(value)
        if not (min_y <= numeric_value <= max_y):
            continue
        if any(abs(existing - numeric_value) < 0.015 for existing in grid_values):
            continue
        grid_values.append(numeric_value)
    for level in sorted(grid_values):
        yy = y_pos(level)
        grid_parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#e7ecf3" stroke-width="1"/>')
        grid_parts.append(f'<text x="{left-10}" y="{yy+4:.1f}" font-size="11" fill="#64748b" text-anchor="end">{level:.2f}</text>')

    start_ts = pd.to_datetime(df["timestamp"].min())
    end_ts = pd.to_datetime(df["timestamp"].max())
    span_seconds = max(0.0, (end_ts - start_ts).total_seconds()) if pd.notna(start_ts) and pd.notna(end_ts) else 0.0

    def x_time(value: object) -> float | None:
        if not span_seconds:
            return None
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        ratio = max(0.0, min(1.0, (parsed - start_ts).total_seconds() / span_seconds))
        return left + ratio * plot_w

    ticks = []
    tick_count = min(7, max(2, width // 220))
    for idx in range(int(tick_count)):
        ratio = idx / max(1, int(tick_count) - 1)
        xx = left + ratio * plot_w
        if span_seconds > 0:
            tick_ts = start_ts + pd.to_timedelta(span_seconds * ratio, unit="s")
            tick_label = html.escape(_format_tick_label(tick_ts, span_seconds))
        else:
            tick_label = str(int(round(ratio * (len(values) - 1))) + 1)
        ticks.append(f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{axis_y}" stroke="#eef2f7" stroke-width="1"/>')
        ticks.append(f'<line x1="{xx:.1f}" y1="{axis_y}" x2="{xx:.1f}" y2="{axis_y+5}" stroke="#cbd5e1" stroke-width="1"/>')
        ticks.append(f'<text x="{xx:.1f}" y="{axis_y+20}" font-size="11" fill="#64748b" text-anchor="middle">{tick_label}</text>')

    threshold_parts = []
    for value, color, label in [
        (thresholds.get("set"), "#16a34a", f"设定 {_num(thresholds.get('set'), 2)}"),
        (thresholds.get("ac_high"), "#f59e0b", f"AC上限 {_num(thresholds.get('ac_high'), 2)}"),
        (thresholds.get("ac_low"), "#f59e0b", f"AC下限 {_num(thresholds.get('ac_low'), 2)}"),
        (thresholds.get("sg_high"), "#ef4444", f"SG上限 {_num(thresholds.get('sg_high'), 2)}"),
        (low_flow.get("lockup_projection"), "#7c3aed", f"锁闭投影 {_num(low_flow.get('lockup_projection'), 2)}"),
    ]:
        if value is None or not (min_y <= float(value) <= max_y):
            continue
        yy = y_pos(float(value))
        threshold_parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="{color}" stroke-width="1.2" stroke-dasharray="6 5"/>')
        threshold_parts.append(f'<text x="{width-right+8}" y="{yy+4:.1f}" font-size="11" fill="{color}">{html.escape(label)}</text>')

    for line in _curve_mean_lines(result):
        value = float(line["value"])
        if not (min_y <= value <= max_y):
            continue
        yy = y_pos(value)
        threshold_parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="{line["color"]}" stroke-width="1.2" stroke-dasharray="3 4"/>')
        threshold_parts.append(f'<text x="{left+8}" y="{yy-6:.1f}" font-size="11" fill="{line["color"]}">{html.escape(line["label"])} {_num(value, 3)}</text>')

    ac_band = ""
    if thresholds.get("ac_high") is not None and thresholds.get("ac_low") is not None:
        high = float(thresholds["ac_high"])
        low = float(thresholds["ac_low"])
        if high >= min_y and low <= max_y:
            band_top = y_pos(min(max_y, high))
            band_bottom = y_pos(max(min_y, low))
            ac_band = f'<rect x="{left}" y="{band_top:.1f}" width="{plot_w}" height="{max(1, band_bottom-band_top):.1f}" fill="rgba(245,158,11,.045)"/>'

    low_flow_parts = []
    for window in (low_flow.get("display_windows") or [])[:5]:
        x1 = x_time(window.get("start"))
        x2 = x_time(window.get("end"))
        if x1 is None or x2 is None:
            continue
        if x2 < x1:
            x1, x2 = x2, x1
        low_flow_parts.append(f'<rect x="{x1:.1f}" y="{top}" width="{max(4, x2-x1):.1f}" height="{plot_h}" fill="rgba(124,58,237,.055)"/>')
        low_flow_parts.append(f'<line x1="{x1:.1f}" y1="{top}" x2="{x1:.1f}" y2="{axis_y}" stroke="rgba(124,58,237,.18)" stroke-width="1"/>')

    band_parts = []
    for segment in _significant_segments(segments, 40):
        x1 = x_pos(int(segment["start"]))
        x2 = x_pos(int(segment["end"]))
        if x1 == x2:
            x2 = x1 + max(4, plot_w / max(1, len(values)))
        color = "rgba(239,68,68,.10)" if segment["state"] == "sg-high" else "rgba(245,158,11,.055)"
        band_parts.append(f'<rect x="{x1:.1f}" y="{top}" width="{max(3, x2-x1):.1f}" height="{plot_h}" fill="{color}"/>')

    marker_parts = []
    marker_groups = {"Pmax样本": 0, "Pmin样本": 0}
    first_marker_by_label: dict[str, dict] = {}
    last_marker_by_label: dict[str, dict] = {}
    for marker in _curve_key_markers(values, result):
        idx = int(marker["idx"])
        xx = x_pos(idx)
        yy = y_pos(float(marker["value"]))
        marker_groups[marker["label"]] = marker_groups.get(marker["label"], 0) + 1
        first_marker_by_label.setdefault(marker["label"], marker)
        last_marker_by_label[marker["label"]] = marker
        marker_parts.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="3.8" fill="{marker["color"]}" stroke="#fff" stroke-width="1.5"/>')

    legend = (
        f'<g font-size="11" fill="#475569">'
        f'<text x="{left}" y="{height-12}">Pmax样本 {marker_groups.get("Pmax样本", 0)} 个，Pmin样本 {marker_groups.get("Pmin样本", 0)} 个；虚线为对应均值；低流量窗口显示前5个；超限片段 {len(segments)} 段；超SG {counts.get("sg-high", 0)} 点；超AC {counts.get("ac-high", 0) + counts.get("ac-low", 0)} 点</text>'
        f'</g>'
    )

    return (
        f'<svg class="pressure-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="white"/>'
        + "".join(grid_parts)
        + f'<line x1="{left}" y1="{axis_y}" x2="{width-right}" y2="{axis_y}" stroke="#cbd5e1" stroke-width="1"/>'
        + "".join(ticks)
        + ac_band
        + "".join(low_flow_parts)
        + "".join(band_parts)
        + "".join(threshold_parts)
        + f'<polyline fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="{points}"/>'
        + "".join(marker_parts)
        + legend
        + "</svg>"
    )


def _findings_md(findings: list[dict]) -> str:
    if not findings:
        return "| 规则 | 异常类型 | 证据 | 疑似原因 | 建议 |\n|---|---|---|---|---|\n| - | 未触发诊断规则 | - | - | 按当前健康等级执行 |"
    rows = ["| 规则 | 异常类型 | 证据 | 疑似原因 | 建议 |", "|---|---|---|---|---|"]
    for item in findings:
        rows.append(
            "| {code} | {name} | {evidence} | {cause} | {maintenance} |".format(
                code=item.get("code", ""),
                name=item.get("name", ""),
                evidence=str(item.get("evidence", "")).replace("|", "/"),
                cause=str(item.get("suspected_cause", "")).replace("|", "/"),
                maintenance=str(item.get("maintenance", "")).replace("|", "/"),
            )
        )
    return "\n".join(rows)


def _baseline_md(ai: dict) -> str:
    features = ai.get("top_features", [])
    if not features:
        return "| 特征 | 当前值 | 健康基线 | 偏离程度 |\n|---|---:|---:|---:|\n| - | - | - | - |"
    rows = ["| 特征 | 当前值 | 健康基线 | 偏离程度 |", "|---|---:|---:|---:|"]
    for item in features:
        rows.append(f"| {item['feature']} | {item['value']} | {item['baseline']} | {item['z']} |")
    return "\n".join(rows)


def _performance_md(performance: dict) -> str:
    items = performance.get("items") or []
    if not items:
        return "| 项目 | 单项等级 | 计算值 | 参照值 | 比值 | 风险分 | 置信度 | 判定方法 |\n|---|---|---:|---|---:|---:|---|---|\n| - | - | - | - | - | - | - | - |"
    rows = [
        "| 项目 | 单项等级 | 计算值 | 参照值 | 比值 | 风险分 | 置信度 | 判定方法 |",
        "|---|---|---:|---|---:|---:|---|---|",
    ]
    for item in items:
        confidence = item.get("confidence") or {}
        rows.append(
            "| {name} | {level}级 {label} | {value} {unit} | {reference} | {ratio} | {risk_score} | {confidence} | {method} |".format(
                name=str(item.get("name", "")).replace("|", "/"),
                value=item.get("value", ""),
                unit=item.get("unit", ""),
                reference=str(item.get("reference", "")).replace("|", "/"),
                ratio=item.get("ratio", ""),
                level=item.get("level", ""),
                label=item.get("label", ""),
                risk_score=item.get("risk_score", ""),
                confidence=f"{confidence.get('label', '')}（{confidence.get('score', '')}）",
                method=str(item.get("method") or item.get("basis", "")).replace("|", "/"),
            )
        )
    return "\n".join(rows)


def _performance_item(performance: dict, code: str) -> dict:
    for item in performance.get("items") or []:
        if item.get("code") == code:
            return item
    return {}


def _performance_card_html(item: dict, fallback_name: str) -> str:
    level = int(item.get("level") or 0)
    confidence = item.get("confidence") or {}
    return f"""
      <div class="metric perf-card level{level}">
        <div class="k">{_escape(item.get('name') or fallback_name)}</div>
        <div class="v">{_escape(item.get('level', ''))}级 {_escape(item.get('label', ''))}</div>
        <div class="sub">计算值 {_escape(item.get('value', ''))}{_escape(item.get('unit', ''))} / 比值 {_escape(item.get('ratio', ''))}</div>
        <div class="sub">风险分 {_escape(item.get('risk_score', ''))} / 置信度 {_escape(confidence.get('label', ''))}</div>
      </div>
    """


def _performance_summary_text(performance: dict) -> str:
    items = performance.get("items") or []
    if not items:
        return "未形成三项性能评价。"
    parts = [f"{item.get('name')}：{item.get('level')}级{item.get('label')}" for item in items]
    return "；".join(parts)


def _performance_evidence_md(performance: dict) -> str:
    rows = ["| 项目 | 校核证据 | 数值 |", "|---|---|---:|"]
    found = False
    for item in performance.get("items") or []:
        for metric in item.get("evidence_metrics") or []:
            found = True
            rows.append(
                f"| {str(item.get('name', '')).replace('|', '/')} | "
                f"{str(metric.get('name', '')).replace('|', '/')} | "
                f"{metric.get('value', '')} {metric.get('unit', '')} |"
            )
    if not found:
        rows.append("| - | - | - |")
    return "\n".join(rows)


def _decision_reasons_md(health: dict) -> str:
    reasons = health.get("decision_reasons") or []
    if not reasons:
        return "| 判定依据 |\n|---|\n| - |"
    rows = ["| 判定依据 |", "|---|"]
    for item in reasons:
        rows.append(f"| {str(item).replace('|', '/')} |")
    return "\n".join(rows)


def _html_table_from_markdown_table(markdown_table: str) -> str:
    lines = [line.strip() for line in markdown_table.splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    body_lines = lines[2:]
    html_rows = ["<table><thead><tr>" + "".join(f"<th>{_escape(h)}</th>" for h in headers) + "</tr></thead><tbody>"]
    for line in body_lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        html_rows.append("<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in cells) + "</tr>")
    html_rows.append("</tbody></table>")
    return "\n".join(html_rows)


def _model_method_label(isolation: dict) -> str:
    method = isolation.get("method", "")
    if method == "sklearn_isolation_forest":
        version = isolation.get("sklearn_version")
        return f"Isolation Forest（scikit-learn {version}）" if version else "Isolation Forest（scikit-learn）"
    if method == "lightweight_isolation_forest":
        return "Isolation Forest（内置轻量实现）"
    if method:
        return str(method)
    return ""


def _overview_table(rows: list[tuple[str, object]]) -> str:
    body = "".join(f"<tr><th>{_escape(k)}</th><td>{_escape(v)}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _overview_metric_evidence(item: dict) -> str:
    metrics = item.get("evidence_metrics") or []
    if not metrics:
        return "<tr><td colspan='2'>暂无校核证据</td></tr>"
    return "".join(
        f"<tr><td>{_escape(metric.get('name'))}</td><td>{_escape(metric.get('value'))} {_escape(metric.get('unit'))}</td></tr>"
        for metric in metrics
    )


def write_overview_html_report(result: dict, data: pd.DataFrame, output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    features = result.get("features") or {}
    health = result.get("health") or {}
    performance = result.get("performance") or {}
    perf_params = performance.get("params") or {}
    items = performance.get("items") or []
    ai = result.get("ai") or {}
    iso = ai.get("isolation_forest") or {}
    knn = ai.get("knn") or {}
    profile = result.get("input_profile") or {}
    findings = result.get("findings") or []
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    station = features.get("station", "调压器")
    safe_station = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(station))
    html_path = output_dir / f"{safe_station}_overview_report.html"

    perf_cards = "".join(
        f"""
        <article class="metric-card level{int(item.get('level') or 1)}">
          <div class="metric-head">
            <h3>{_escape(item.get('name'))} <span>{_escape(item.get('code'))}</span></h3>
            <strong>{_escape(item.get('level'))}级 {_escape(item.get('label'))}</strong>
          </div>
          <div class="metric-value">{_escape(item.get('value'))} {_escape(item.get('unit'))}</div>
          <p>{_escape(item.get('basis'))}</p>
          {_overview_table([
              ("参照值", item.get("reference", "")),
              ("判定比值", item.get("ratio", "")),
              ("风险分", item.get("risk_score", "")),
              ("置信度", f"{(item.get('confidence') or {}).get('label', '')} / {(item.get('confidence') or {}).get('score', '')}"),
              ("判定方法", item.get("method", "")),
          ])}
          <details>
            <summary>校核证据</summary>
            <table><tr><th>证据</th><th>值</th></tr>{_overview_metric_evidence(item)}</table>
          </details>
        </article>
        """
        for item in items
    )

    aux_table = _overview_table(
        [
            ("规则等级", (health.get("evidence") or {}).get("rule_level", "")),
            ("综合风险分", health.get("risk_score", "")),
            ("三项性能风险分", health.get("performance_score", "")),
            ("辅助证据分", health.get("auxiliary_score", "")),
            ("Isolation Forest", f"{iso.get('score', '')} / {iso.get('band_label', '')} / P{iso.get('percentile', '')}"),
            ("KNN", f"{knn.get('score', '')} / {knn.get('band_label', '')} / P{knn.get('percentile', '')}"),
            ("基线偏离分", ai.get("baseline_score", "")),
            ("趋势分", health.get("trend_score", "")),
        ]
    )
    decision_items = "".join(f"<li>{_escape(reason)}</li>" for reason in (health.get("decision_reasons") or [])) or "<li>暂无判定依据</li>"
    finding_rows = "".join(
        f"<tr><td>{_escape(item.get('code'))}</td><td>{_escape(item.get('name'))}</td><td>{_escape(item.get('evidence'))}</td><td>{_escape(item.get('maintenance'))}</td></tr>"
        for item in findings
    ) or "<tr><td colspan='4'>未触发明确压力安全规则</td></tr>"
    block_rows = "".join(
        f"<tr><td>{_escape(block.get('station'))}</td><td>{_escape(block.get('timestamp_source'))}</td><td>{_escape(block.get('pressure_column'))}</td><td>{_escape(block.get('seat_leakage_column'))}</td><td>{_escape(block.get('valid_rows'))}</td><td>{_escape(block.get('start'))} 至 {_escape(block.get('end'))}</td></tr>"
        for block in (profile.get("blocks") or [])
    ) or "<tr><td colspan='6'>无导入识别信息</td></tr>"

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(station)} 诊断概览报告</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #eef3f8; color: #111827; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; line-height: 1.65; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
header, section {{ background: #fff; border: 1px solid #dbe3ee; border-radius: 18px; padding: 24px; margin-bottom: 18px; }}
h1 {{ margin: 0 0 10px; font-size: 28px; }}
h2 {{ margin: 0 0 16px; font-size: 20px; }}
h3 {{ margin: 0; font-size: 16px; }}
p {{ color: #667085; margin: 8px 0; }}
.hero {{ display: grid; grid-template-columns: 1.05fr .95fr; gap: 18px; align-items: stretch; }}
.conclusion {{ background: linear-gradient(135deg, #0f766e, #1d4ed8); color: white; border-radius: 18px; padding: 24px; }}
.conclusion p {{ color: #dff7ff; }}
.grade {{ font-size: 42px; font-weight: 800; margin: 10px 0; }}
.grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.metric-card {{ border: 1px solid #dbe3ee; background: #f8fafc; border-radius: 16px; padding: 18px; }}
.metric-card.level1, .metric-card.level2 {{ border-color: #9ee7d3; }}
.metric-card.level3 {{ border-color: #ffd78a; }}
.metric-card.level4, .metric-card.level5 {{ border-color: #ffb4ad; }}
.metric-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
.metric-head span {{ color: #667085; }}
.metric-head strong {{ color: #111827; white-space: nowrap; }}
.metric-value {{ font-size: 26px; font-weight: 800; margin: 14px 0 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 10px; }}
th, td {{ border-bottom: 1px solid #e8eef5; padding: 10px 12px; text-align: left; vertical-align: top; }}
th {{ color: #667085; background: #f8fafc; font-weight: 700; }}
.chart {{ overflow: auto; border: 1px solid #dbe3ee; border-radius: 16px; padding: 12px; background: #fff; }}
.rules td:nth-child(4) {{ color: #e53935; font-weight: 700; }}
details {{ margin-top: 12px; }}
summary {{ cursor: pointer; color: #1a73e8; font-weight: 700; }}
@media print {{ body {{ background: white; }} main {{ padding: 0; max-width: none; }} section, header {{ break-inside: avoid; }} }}
@media (max-width: 900px) {{ .hero, .grid, .two {{ grid-template-columns: 1fr; }} main {{ padding: 14px; }} }}
</style>
</head>
<body>
<main>
  <header class="hero">
    <div class="conclusion">
      <h1>{_escape(station)} 诊断概览报告</h1>
      <div class="grade">{_escape(health.get('level'))}级 {_escape(health.get('label'))}</div>
      <p>{_escape(_level_summary(int(health.get('level') or 1), str(health.get('label') or ''), findings))}</p>
      <p>生成时间：{_escape(generated_at)}；数据范围：{_escape(features.get('start'))} 至 {_escape(features.get('end'))}</p>
    </div>
    <div>
      {_overview_table([
          ("样本数量", features.get("count", "")),
          ("综合风险分", health.get("risk_score", "")),
          ("三项性能综合", f"{(performance.get('overall') or {}).get('level', '')}级 {(performance.get('overall') or {}).get('label', '')}"),
          ("设定压力", f"{perf_params.get('set_pressure_kpa', '')} KPa"),
          ("AC / SG", f"{perf_params.get('ac_percent', '')}% / {perf_params.get('sg_percent', '')}%"),
          ("AC / SG换算限值", f"{perf_params.get('ac_limit_kpa', '')} / {perf_params.get('sg_limit_kpa', '')} KPa"),
      ])}
    </div>
  </header>

  <section>
    <h2>核心参考</h2>
    <div class="grid">{perf_cards}</div>
  </section>

  <section>
    <h2>压力曲线</h2>
    <div class="chart">{sparkline_svg(data, result)}</div>
  </section>

  <section>
    <h2>辅助参考</h2>
    <div class="two">
      <div>{aux_table}</div>
      <div><h3>判定依据</h3><ul>{decision_items}</ul></div>
    </div>
  </section>

  <section>
    <h2>规则触发</h2>
    <table class="rules"><tr><th>规则</th><th>类型</th><th>证据</th><th>建议</th></tr>{finding_rows}</table>
  </section>

  <section>
    <h2>导入识别</h2>
    {_overview_table([
        ("文件", profile.get("file_name", "")),
        ("类型", profile.get("file_type", "")),
        ("原始行/列", f"{profile.get('raw_rows', 0)} / {profile.get('raw_columns', 0)}"),
        ("有效/无效", f"{profile.get('valid_rows', 0)} / {profile.get('invalid_rows', 0)}"),
    ])}
    <table><tr><th>站点</th><th>时间来源</th><th>压力列</th><th>泄漏量列</th><th>有效样本</th><th>时间范围</th></tr>{block_rows}</table>
  </section>
</main>
</body>
</html>"""
    html_path.write_text(html_doc, encoding="utf-8")
    return str(html_path)


def _snapshot_level_class(level: object) -> str:
    try:
        value = int(level)
    except (TypeError, ValueError):
        return "good"
    if value <= 1:
        return "good"
    if value <= 2:
        return "success"
    if value <= 3:
        return "warn"
    return "danger"


def write_main_snapshot_html_report(result: dict, data: pd.DataFrame, output_dir: Path) -> str:
    """Export the same diagnostic content shown in the right-side web workspace."""
    output_dir.mkdir(parents=True, exist_ok=True)
    features = result.get("features") or {}
    health = result.get("health") or {}
    performance = result.get("performance") or {}
    items = performance.get("items") or []
    ai = result.get("ai") or {}
    iso = ai.get("isolation_forest") or {}
    knn = ai.get("knn") or {}
    ev = health.get("evidence") or {}
    profile = result.get("input_profile") or {}
    findings = result.get("findings") or []
    links_station = features.get("station", "调压器")
    safe_station = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(links_station))
    html_path = output_dir / f"{safe_station}_overview_report.html"

    def item_by_code(code: str) -> dict:
        for item in items:
            if item.get("code") == code:
                return item
        return {}

    def metric_card(item: dict) -> str:
        cls = _snapshot_level_class(item.get("level"))
        confidence = item.get("confidence") or {}
        evidence_rows = "".join(
            f"<tr><td>{_escape(metric.get('name'))}</td><td>{_escape(metric.get('value'))} {_escape(metric.get('unit'))}</td></tr>"
            for metric in (item.get("evidence_metrics") or [])
        ) or "<tr><td colspan='2'>暂无校核证据</td></tr>"
        return f"""
        <details class="metric-details">
          <summary>
            <div class="metric-row">
              <div class="metric-indicator {cls}"></div>
              <div class="metric-info">
                <div class="metric-name">{_escape(item.get('name'))} ({_escape(item.get('code'))})</div>
                <div class="metric-desc">{_escape(confidence.get('label'))}置信度 { _escape(confidence.get('score')) }{('，' + _escape(confidence.get('reason'))) if confidence.get('reason') else ''}</div>
              </div>
              <div class="metric-score">
                <div class="metric-val">{_escape(item.get('value'))} {_escape(item.get('unit'))}</div>
                <div class="metric-level {cls}">{_escape(item.get('level'))}级 {_escape(item.get('label'))}</div>
              </div>
              <span class="metric-expand">+</span>
            </div>
          </summary>
          <div class="metric-detail-body">
            <div class="metric-detail-grid">
              <div class="metric-detail-chip"><span>计算值</span><strong>{_escape(item.get('value'))} {_escape(item.get('unit'))}</strong></div>
              <div class="metric-detail-chip"><span>参照值</span><strong>{_escape(item.get('reference'))}</strong></div>
              <div class="metric-detail-chip"><span>判定占比</span><strong>{_num(float(item.get('ratio') or 0) * 100, 1)}%</strong></div>
              <div class="metric-detail-chip"><span>占比公式</span><strong>({_escape(item.get('ratio_formula') or '计算值 / 参考限值')}) × 100%</strong></div>
              <div class="metric-detail-chip"><span>风险分</span><strong>{_escape(item.get('risk_score'))}</strong></div>
              <div class="metric-detail-chip"><span>置信度</span><strong>{_escape(confidence.get('label'))} / {_escape(confidence.get('score'))}</strong></div>
              <div class="metric-detail-chip"><span>数据状态</span><strong>{_escape('压力采样估算' if item.get('confirmation_status') == 'pressure_surrogate' else '低流量窗口估算' if item.get('confirmation_status') == 'low_flow_surrogate' else '关闭压力字段判定' if item.get('confirmation_status') == 'formal_closing_pressure' else '泄漏量字段判定' if item.get('confirmation_status') == 'formal_leakage_amount' else '压力采样判定')}</strong></div>
              <div class="metric-detail-chip"><span>分级区间</span><strong>{_escape(item.get('threshold_desc'))}</strong></div>
            </div>
            <p><strong>定义：</strong>{_escape(item.get('definition'))}</p>
            <p><strong>计算公式：</strong>{_escape(item.get('formula'))}</p>
            <p><strong>判定方法：</strong>{_escape(item.get('method'))}</p>
            <p><strong>判定依据：</strong>{_escape(item.get('basis'))}</p>
            <table><tr><th>校核证据</th><th>数值</th></tr>{evidence_rows}</table>
          </div>
        </details>
        """

    p01, p02, p03 = item_by_code("P01"), item_by_code("P02"), item_by_code("P03")
    score_items = [
        ("三项性能", health.get("performance_score"), "primary"),
        ("规则", health.get("rule_score"), "danger"),
        ("IF", iso.get("score") or health.get("model_score"), "warning"),
        ("KNN", knn.get("score") or health.get("knn_score"), "muted"),
        ("基线", health.get("baseline_score"), "success"),
    ]
    evidence_items = "".join(
        f"""
        <div class="evidence-item">
          <div class="evidence-item-head"><span>{_escape(label)}</span><strong>{_num(float(value or 0), 2)}</strong></div>
          <div class="evidence-track"><span class="{cls}" style="width:{max(0, min(100, float(value or 0) * 100)):.1f}%"></span></div>
        </div>
        """
        for label, value, cls in score_items
    )
    decision_rows = "".join(f"<tr><td>{_escape(reason)}</td></tr>" for reason in (health.get("decision_reasons") or [])) or "<tr><td>暂无判定依据</td></tr>"
    block_rows = "".join(
        f"<tr><td>{_escape(block.get('station'))}</td><td>{_escape(block.get('timestamp_source'))}</td><td>{_escape(block.get('pressure_column'))}</td><td>{_escape(block.get('valid_rows'))}</td><td>{_escape(block.get('start'))} 至 {_escape(block.get('end'))}</td></tr>"
        for block in (profile.get("blocks") or [])
    ) or "<tr><td colspan='5'>无导入识别信息</td></tr>"
    max_severity = max([int(item.get("severity") or 0) for item in findings], default=0)
    rule_rows = "".join(
        f"""
        <div class="rule-item fail">
          <div class="rule-code"><span><b>{_escape(item.get('code'))}</b> {_escape(item.get('name'))}</span><em>{_escape(item.get('severity'))}级</em></div>
          <div class="rule-desc">{_escape(item.get('evidence'))}</div>
          <div class="rule-status">● {_escape(item.get('maintenance'))}</div>
        </div>
        """
        for item in findings
    ) or """
        <div class="rule-item">
          <div class="rule-code">未触发明确规则</div>
          <div class="rule-desc">当前数据未命中压力安全底线规则，最终结论以三项核心性能指标为主。</div>
          <div class="rule-status pass">● 正常</div>
        </div>
    """

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(links_station)} 诊断概览</title>
<style>
:root {{
  --bg:#f7faff; --surface:#fff; --surface-2:#fbfdff; --ink:#111827; --muted:#667085; --soft:#98a2b3; --line:#e3ebf5; --line-soft:#f1f5fa;
  --primary:#1a73e8; --primary-light:#e8f0fe; --success:#16a34a; --success-light:#e8f5e9; --warning:#f5b400; --warning-light:#fff4d6; --danger:#ea4335; --danger-light:#fde8e8;
  --shadow-soft:0 8px 22px rgba(17,24,39,.05); --shadow-card:0 14px 34px rgba(17,24,39,.08);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Microsoft YaHei","Segoe UI",sans-serif; }}
.main {{ max-width:1240px; margin:0 auto; padding:24px; display:flex; flex-direction:column; gap:20px; }}
.page-header,.kpi-card,.main-card,.rules-panel {{ background:var(--surface); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow-soft); }}
.page-header {{ padding:22px 24px; display:flex; justify-content:space-between; gap:20px; }}
.page-title {{ font-size:24px; margin:0; }}
.page-meta,.card-meta,.metric-desc,.aux-lbl,.chart-meta {{ color:var(--soft); font-size:13px; }}
.kpi-row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:20px; }}
.kpi-card {{ padding:20px; transition:box-shadow .15s, transform .15s; }}
.kpi-card:hover {{ box-shadow:var(--shadow-card); transform:translateY(-2px); }}
.kpi-label {{ color:var(--muted); font-size:13px; }}
.kpi-value-row {{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:10px; }}
.health-badge,.kpi-badge,.metric-level,.rp-count {{ display:inline-flex; padding:6px 10px; border-radius:8px; font-weight:700; }}
.health-badge.l1,.kpi-badge.good,.metric-level.good {{ background:var(--primary-light); color:var(--primary); }}
.health-badge.l2,.kpi-badge.success,.metric-level.success {{ background:var(--success-light); color:var(--success); }}
.health-badge.l3,.kpi-badge.warn,.metric-level.warn {{ background:var(--warning-light); color:#b76100; }}
.health-badge.l4,.health-badge.l5,.kpi-badge.danger,.metric-level.danger {{ background:var(--danger-light); color:var(--danger); }}
.kpi-value,.metric-val {{ font-size:26px; font-weight:800; font-family:Consolas,monospace; }}
.kpi-sub {{ color:var(--soft); font-size:12px; margin-top:10px; }}
.evidence-strip {{ padding:16px 18px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
.evidence-label {{ color:var(--muted); font-size:13px; font-weight:700; }}
.evidence-items {{ flex:1; display:grid; grid-template-columns:repeat(5,1fr); gap:10px; min-width:420px; }}
.evidence-item {{ background:var(--surface-2); border:1px solid var(--line-soft); border-radius:10px; padding:10px; }}
.evidence-item-head {{ display:flex; justify-content:space-between; color:var(--muted); font-size:12px; }}
.evidence-track {{ height:6px; background:#e8edf3; border-radius:999px; overflow:hidden; margin-top:8px; }}
.evidence-track span {{ display:block; height:100%; border-radius:999px; }}
.evidence-track .primary {{ background:var(--primary); }} .evidence-track .danger {{ background:var(--danger); }} .evidence-track .warning {{ background:var(--warning); }} .evidence-track .muted {{ background:var(--soft); }} .evidence-track .success {{ background:var(--success); }}
.detail-row {{ display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:20px; }}
.main-card,.rules-panel {{ padding:24px; }}
.card-header,.rp-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }}
.metric-grid {{ display:flex; flex-direction:column; gap:14px; }}
.metric-details {{ background:var(--surface-2); border:1px solid transparent; border-radius:12px; overflow:hidden; }}
.metric-details[open] {{ background:#fff; border-color:var(--line); }}
.metric-details summary {{ list-style:none; cursor:pointer; }}
.metric-details summary::-webkit-details-marker {{ display:none; }}
.metric-row {{ display:flex; align-items:center; gap:16px; padding:16px; }}
.metric-details[open] .metric-row {{ border-bottom:1px solid var(--line-soft); }}
.metric-indicator {{ width:4px; height:44px; border-radius:3px; }}
.metric-indicator.good {{ background:var(--primary); }} .metric-indicator.success {{ background:var(--success); }} .metric-indicator.warn {{ background:var(--warning); }} .metric-indicator.danger {{ background:var(--danger); }}
.metric-info {{ flex:1; }} .metric-name {{ font-weight:800; }} .metric-score {{ text-align:right; }}
.metric-expand {{ width:28px; height:28px; display:grid; place-items:center; border-radius:999px; background:#edf1f6; color:var(--muted); font-weight:800; }}
.metric-details[open] .metric-expand {{ transform:rotate(45deg); background:var(--primary-light); color:var(--primary); }}
.metric-detail-body {{ padding:16px 18px 18px 40px; display:grid; gap:14px; }}
.metric-detail-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }}
.metric-detail-chip {{ background:var(--surface-2); border:1px solid var(--line-soft); border-radius:10px; padding:10px 12px; }}
.metric-detail-chip span {{ display:block; color:var(--soft); font-size:11px; font-weight:700; }} .metric-detail-chip strong {{ display:block; margin-top:4px; }}
.aux-bar {{ height:1px; background:var(--line-soft); margin:18px 0 12px; }}
.aux-title {{ color:var(--soft); font-size:12px; font-weight:800; }}
.aux-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:10px; }}
.aux-item {{ background:var(--surface-2); border-radius:10px; padding:12px; text-align:center; }}
.aux-val {{ font-size:20px; font-weight:800; font-family:Consolas,monospace; }}
.chart-panel {{ margin-top:16px; background:var(--surface-2); border:1px solid var(--line-soft); border-radius:12px; padding:16px; }}
.chart-panel-head {{ display:flex; justify-content:space-between; margin-bottom:12px; }}
.chart-title {{ font-weight:800; }}
.chart-panel {{ overflow-x:auto; }}
.chart-panel svg {{ min-width:1600px; width:1800px; max-width:none; height:auto; display:block; background:#fff; border-radius:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line-soft); text-align:left; vertical-align:top; }}
th {{ background:var(--surface-2); color:var(--soft); }}
.subpanel {{ margin-top:18px; }}
.rp-count.clear {{ background:var(--success-light); color:var(--success); }} .rp-count.alert {{ background:var(--warning-light); color:#b76100; }} .rp-count.danger {{ background:var(--danger-light); color:var(--danger); }}
.rule-summary {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:16px; }}
.rule-summary-card {{ background:var(--surface-2); border-radius:10px; padding:14px 10px; text-align:center; }}
.rule-summary-value {{ font-size:22px; font-weight:800; font-family:Consolas,monospace; }} .rule-summary-label {{ color:var(--soft); font-size:12px; }}
.rule-list {{ display:flex; flex-direction:column; gap:16px; }}
.rule-item {{ background:var(--surface-2); border:1px solid var(--line-soft); border-radius:12px; padding:16px; }}
.rule-item.fail {{ background:#fff8f7; border-color:#ffc4bd; }}
.rule-code {{ display:flex; justify-content:space-between; font-weight:800; }} .rule-code em {{ color:var(--danger); font-style:normal; background:var(--danger-light); border-radius:999px; padding:3px 8px; }}
.rule-desc {{ color:var(--muted); margin-top:10px; line-height:1.7; }} .rule-status {{ color:var(--danger); font-weight:800; margin-top:10px; }} .rule-status.pass {{ color:var(--success); }}
@media print {{ body {{ background:#fff; }} .main {{ max-width:none; padding:0; }} .page-header,.kpi-card,.main-card,.rules-panel {{ break-inside:avoid; }} }}
@media (max-width:980px) {{ .kpi-row,.detail-row,.metric-detail-grid,.aux-grid,.evidence-items {{ grid-template-columns:1fr; min-width:0; }} }}
</style>
</head>
<body>
<main class="main">
  <div class="main-card">
    <section class="report-section">
      <div class="report-section-title"><h2>导入识别</h2><span>文件结构与字段识别</span></div>
      <table><tr><th>文件</th><td>{_escape(profile.get('file_name'))}</td><th>类型</th><td>{_escape(profile.get('file_type'))}</td></tr><tr><th>原始行/列</th><td>{_escape(profile.get('raw_rows'))}/{_escape(profile.get('raw_columns'))}</td><th>有效/无效</th><td>{_escape(profile.get('valid_rows'))}/{_escape(profile.get('invalid_rows'))}</td></tr><tr><th>站点</th><th>时间来源</th><th>压力列</th><th>有效样本</th><th>时间范围</th></tr>{block_rows}</table>
    </section>
  </div>
  <div class="page-header">
    <div>
      <h1 class="page-title">诊断概览</h1>
      <div class="page-meta">{_escape(result.get('source_filename') or '')} · {_escape(features.get('start'))} - {_escape(features.get('end'))} · {_escape(features.get('count'))} 样本</div>
    </div>
    <div class="page-meta">站点：{_escape(links_station)}</div>
  </div>

  <div class="kpi-row">
    <div class="kpi-card"><div class="kpi-label">健康等级</div><div class="kpi-value-row"><div class="health-badge l{_escape(health.get('level'))}">{_escape(health.get('label'))}</div><div class="kpi-value">{_num(float(health.get('risk_score') or 0), 2)}</div></div><div class="kpi-sub">综合风险分 · 等级 {_escape(health.get('level'))}/5</div></div>
    <div class="kpi-card"><div class="kpi-label">稳压性能 P01</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p01.get('value') or 0), 2)}%</div><div class="kpi-badge {_snapshot_level_class(p01.get('level'))}">{_escape(p01.get('label'))}</div></div><div class="kpi-sub">实际AC / 出厂AC</div></div>
    <div class="kpi-card"><div class="kpi-label">关闭压力 P02</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p02.get('value') or 0), 2)}%</div><div class="kpi-badge {_snapshot_level_class(p02.get('level'))}">{_escape(p02.get('label'))}</div></div><div class="kpi-sub">实际SG / 出厂SG</div></div>
    <div class="kpi-card"><div class="kpi-label">阀座密封 P03</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p03.get('value') or 0), 1 if p03.get('unit') == '%' else 2)}{_escape('%' if p03.get('unit') == '%' else '')}</div><div class="kpi-badge {_snapshot_level_class(p03.get('level'))}">{_escape(p03.get('label'))}</div></div><div class="kpi-sub">{_escape('密封爬升指数，非实测泄漏量' if p03.get('unit') == '%' else '实测/导入泄漏量')}</div></div>
  </div>

  <div class="detail-row">
    <div class="main-card">
      <div class="card-header"><strong>核心性能指标详情</strong><span class="card-meta">站点：{_escape(links_station)} · {_escape(features.get('start'))}</span></div>
      <div class="metric-grid">{''.join(metric_card(item) for item in items)}</div>
      <div class="aux-bar"></div>
      <span class="aux-title">辅助参考指标</span>
      <div class="aux-grid">
        <div class="aux-item"><div class="aux-val">{_num(features.get('mean'), 2)}</div><div class="aux-lbl">均值 kPa</div></div>
        <div class="aux-item"><div class="aux-val">{_num(features.get('std'), 3)}</div><div class="aux-lbl">标准差 σ</div></div>
        <div class="aux-item"><div class="aux-val">{_num((float(features.get('std') or 0) / max(float(features.get('mean') or 1), 1e-9)) * 100, 1)}%</div><div class="aux-lbl">变异系数 CV</div></div>
        <div class="aux-item"><div class="aux-val">{_escape(features.get('count'))}</div><div class="aux-lbl">数据点数</div></div>
      </div>
      <div class="chart-panel"><div class="chart-panel-head"><span class="chart-title">压力曲线</span><span class="chart-meta">{_num(features.get('min'), 2)} - {_num(features.get('max'), 2)} KPa / {_escape(features.get('count'))} 点</span></div>{sparkline_svg(data, result)}</div>
      <div class="subpanel"><h3>辅助参考</h3><table><tr><th>规则等级</th><td>{_escape(ev.get('rule_level'))}</td><th>综合风险分</th><td>{_num(float(health.get('risk_score') or 0), 4)}</td></tr><tr><th>Isolation Forest</th><td>{_num(float(iso.get('score') or 0), 4)} / {_escape(iso.get('band_label'))} / P{_escape(iso.get('percentile'))}</td><th>KNN</th><td>{_num(float(knn.get('score') or 0), 4)} / {_escape(knn.get('band_label'))} / P{_escape(knn.get('percentile'))}</td></tr><tr><th>基线等级</th><td>{_escape(ev.get('baseline_level'))}</td><th>趋势等级</th><td>{_escape(ev.get('trend_level'))}</td></tr></table><table><tr><th>说明</th></tr>{decision_rows}</table></div>
      <div class="subpanel"><h3>导入识别</h3><table><tr><th>文件</th><td>{_escape(profile.get('file_name'))}</td><th>类型</th><td>{_escape(profile.get('file_type'))}</td></tr><tr><th>原始行/列</th><td>{_escape(profile.get('raw_rows'))}/{_escape(profile.get('raw_columns'))}</td><th>有效/无效</th><td>{_escape(profile.get('valid_rows'))}/{_escape(profile.get('invalid_rows'))}</td></tr><tr><th>站点</th><th>时间来源</th><th>压力列</th><th>有效样本</th><th>时间范围</th></tr>{block_rows}</table></div>
    </div>

    <div class="rules-panel">
      <div class="rp-header"><strong>规则触发</strong><span class="rp-count {'clear' if not findings else ('alert' if len(findings) <= 2 else 'danger')}">{len(findings)} 条触发</span></div>
      <div class="rule-summary"><div class="rule-summary-card"><div class="rule-summary-value">{len(findings)}</div><div class="rule-summary-label">触发规则</div></div><div class="rule-summary-card"><div class="rule-summary-value">{max_severity or '—'}</div><div class="rule-summary-label">最高等级</div></div><div class="rule-summary-card"><div class="rule-summary-value">{max(0, 8 - len(findings))}</div><div class="rule-summary-label">未触发规则</div></div></div>
      <div class="rule-list">{rule_rows}</div>
    </div>
  </div>
</main>
</body>
</html>"""
    html_path.write_text(html_doc, encoding="utf-8")
    return str(html_path)


def write_main_snapshot_html_report(result: dict, data: pd.DataFrame, output_dir: Path) -> str:
    """Export the current web workspace view without the import sidebar."""
    output_dir.mkdir(parents=True, exist_ok=True)
    features = result.get("features") or {}
    health = result.get("health") or {}
    performance = result.get("performance") or {}
    items = performance.get("items") or []
    findings = result.get("findings") or []
    ai = result.get("ai") or {}
    iso = ai.get("isolation_forest") or {}
    knn = ai.get("knn") or {}
    ev = health.get("evidence") or {}
    profile = result.get("input_profile") or {}
    station = str(features.get("station") or "调压器")
    safe_station = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in station)
    html_path = output_dir / f"{safe_station}_overview_report.html"

    def item_by_code(code: str) -> dict:
        for item in items:
            if item.get("code") == code:
                return item
        return {}

    def level_class(level: object) -> str:
        try:
            value = int(level or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 1:
            return "good"
        if value == 2:
            return "success"
        if value == 3:
            return "warn"
        return "danger"

    def metric_value(item: dict) -> str:
        if item.get("value") is None:
            return "-"
        unit = item.get("unit") or ""
        return f"{_num(float(item.get('value') or 0), 4)} {unit}".strip()

    def metric_card(item: dict) -> str:
        cls = level_class(item.get("level"))
        confidence = item.get("confidence") or {}
        evidence_rows = "".join(
            f"<tr><td>{_escape(metric.get('name'))}</td><td>{_escape(metric.get('value'))} {_escape(metric.get('unit'))}</td></tr>"
            for metric in (item.get("evidence_metrics") or [])
        ) or "<tr><td colspan='2'>暂无校核证据</td></tr>"
        status = (
            "压力采样估算"
            if item.get("confirmation_status") == "pressure_surrogate"
            else "低流量窗口估算"
            if item.get("confirmation_status") == "low_flow_surrogate"
            else "关闭压力字段判定"
            if item.get("confirmation_status") == "formal_closing_pressure"
            else "泄漏量字段判定"
            if item.get("confirmation_status") == "formal_leakage_amount"
            else "压力采样判定"
        )
        return f"""
          <details class="metric-details">
            <summary class="metric-summary">
              <div class="metric-row">
                <div class="metric-indicator {cls}"></div>
                <div class="metric-info">
                  <div class="metric-name">{_escape(item.get('name'))} ({_escape(item.get('code'))})</div>
                  <div class="metric-desc">{_escape(confidence.get('label'))}置信度 / {_escape(confidence.get('score'))}{('，' + _escape(confidence.get('reason'))) if confidence.get('reason') else ''}</div>
                </div>
                <div class="metric-score">
                  <div class="metric-val">{metric_value(item)}</div>
                  <div class="metric-level {cls}">{_escape(item.get('level'))}级 {_escape(item.get('label'))}</div>
                </div>
                <span class="metric-expand">+</span>
              </div>
            </summary>
            <div class="metric-detail-body">
              <div class="metric-detail-grid">
                <div class="metric-detail-chip"><span>计算值</span><strong>{metric_value(item)}</strong></div>
                <div class="metric-detail-chip"><span>参考值</span><strong>{_escape(item.get('reference'))}</strong></div>
                <div class="metric-detail-chip"><span>判定占比</span><strong>{_num(float(item.get('ratio') or 0) * 100, 1)}%</strong></div>
                <div class="metric-detail-chip"><span>占比公式</span><strong>({_escape(item.get('ratio_formula') or '计算值 / 参考限值')}) x 100%</strong></div>
                <div class="metric-detail-chip"><span>风险分</span><strong>{_escape(item.get('risk_score'))}</strong></div>
                <div class="metric-detail-chip"><span>数据状态</span><strong>{_escape(status)}</strong></div>
                <div class="metric-detail-chip"><span>分级区间</span><strong>{_escape(item.get('threshold_desc'))}</strong></div>
              </div>
              <p><strong>定义：</strong>{_escape(item.get('definition'))}</p>
              <p><strong>计算公式：</strong>{_escape(item.get('formula'))}</p>
              <p><strong>判定方法：</strong>{_escape(item.get('method'))}</p>
              <p><strong>判定依据：</strong>{_escape(item.get('basis'))}</p>
              <table><tr><th>校核证据</th><th>数值</th></tr>{evidence_rows}</table>
            </div>
          </details>
        """

    rule_meta = {
        "R01": ("稳压超限", "出口压力超出设定压力+AC允许范围"),
        "R02": ("关闭压力高", "关闭压力字段或低流量锁闭投影超过SG上限"),
        "R03": ("阀座泄漏", "泄漏量超限或密封爬升指数异常"),
        "R04": ("喘振波动", "压力波动幅值与频率异常"),
        "R05": ("趋势漂移", "长期压力趋势偏离正常范围"),
        "R06": ("夜间异常", "夜间低压时段压力偏高"),
        "R07": ("波动率异常", "压力波动率超出正常范围"),
        "R08": ("静压漂移", "非流动期静压持续漂移"),
    }
    hit_map = {item.get("code"): item for item in findings}
    ordered_codes = sorted(rule_meta, key=lambda code: 0 if code in hit_map else 1)
    rule_rows = []
    for code in ordered_codes:
        hit = hit_map.get(code)
        name, desc = rule_meta[code]
        if hit:
            rule_rows.append(
                f"""
                <div class="rule-item hit">
                  <div class="rule-code"><span><code>{_escape(code)}</code> {_escape(hit.get('name') or name)}</span><em>{_escape(hit.get('severity'))}级</em></div>
                  <div class="rule-desc">{_escape(hit.get('evidence'))}</div>
                  <div class="rule-status fail"><span></span>{_escape(hit.get('maintenance') or '建议处理')}</div>
                </div>
                """
            )
        else:
            rule_rows.append(
                f"""
                <div class="rule-item ok">
                  <div class="rule-code"><span><code>{_escape(code)}</code> {_escape(name)}</span></div>
                  <div class="rule-desc">{_escape(desc)}</div>
                  <div class="rule-status pass"><span></span>正常</div>
                </div>
                """
            )

    p01 = item_by_code("P01")
    p02 = item_by_code("P02")
    p03 = item_by_code("P03")
    max_severity = max([int(item.get("severity") or 0) for item in findings], default=0)
    health_level = int(health.get("level") or 1)
    health_class = f"l{health_level}"
    aux_cv = (float(features.get("std") or 0) / max(float(features.get("mean") or 1), 1e-9)) * 100
    decision_rows = "".join(f"<tr><td>{_escape(reason)}</td></tr>" for reason in (health.get("decision_reasons") or [])) or "<tr><td>暂无判定依据</td></tr>"
    block_rows = "".join(
        f"<tr><td>{_escape(block.get('station'))}</td><td>{_escape(block.get('timestamp_source'))}</td><td>{_escape(block.get('pressure_column'))}</td><td>{_escape(block.get('valid_rows'))}</td><td>{_escape(block.get('start'))} 至 {_escape(block.get('end'))}</td></tr>"
        for block in (profile.get("blocks") or [])
    ) or "<tr><td colspan='5'>无导入识别信息</td></tr>"

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(station)} 诊断概览</title>
<style>
:root {{
  --bg:#f7faff; --surface:#fff; --surface-2:#fbfdff; --ink:#111827; --ink-2:#374151; --muted:#6b7280; --soft:#9ca3af;
  --line:#e5e7eb; --line-soft:#f3f4f6; --primary:#1a73e8; --primary-light:#eef4ff; --success:#34a853; --success-light:#e8f5e9;
  --warning:#fbbc04; --warning-light:#fff8eb; --warning-text:#c26a00; --danger:#ea4335; --danger-light:#fde8e8;
  --radius:12px; --radius-sm:8px; --radius-lg:16px; --shadow-card:0 14px 34px rgba(17,24,39,.08); --shadow-soft:0 8px 22px rgba(17,24,39,.05);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Microsoft YaHei","Noto Sans SC","Segoe UI",sans-serif; }}
.main {{ padding:20px; display:flex; flex-direction:column; gap:20px; }}
.page-header {{ display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }}
.page-title {{ font-size:22px; font-weight:700; margin:0; }}
.page-meta,.card-meta {{ font-size:13px; color:var(--soft); margin-top:4px; }}
.kpi-row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:20px; }}
.kpi-card,.main-card,.rules-panel {{ background:var(--surface); border:1px solid rgba(219,227,238,.9); border-radius:var(--radius-lg); box-shadow:var(--shadow-soft); }}
.kpi-card {{ padding:24px; display:flex; flex-direction:column; gap:12px; }}
.kpi-label {{ font-size:12px; font-weight:700; color:var(--soft); }}
.kpi-value-row {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.kpi-value {{ font-size:32px; font-weight:800; color:var(--ink); font-family:Consolas,monospace; }}
.kpi-sub {{ color:var(--soft); font-size:12px; }}
.kpi-badge,.metric-level,.health-badge,.rp-count {{ display:inline-flex; align-items:center; border-radius:8px; padding:6px 10px; font-size:12px; font-weight:800; }}
.health-badge {{ font-size:18px; }}
.health-badge.l1,.kpi-badge.good,.metric-level.good {{ background:var(--primary-light); color:var(--primary); }}
.health-badge.l2,.kpi-badge.success,.metric-level.success {{ background:var(--success-light); color:var(--success); }}
.health-badge.l3,.kpi-badge.warn,.metric-level.warn {{ background:var(--warning-light); color:var(--warning-text); }}
.health-badge.l4,.health-badge.l5,.kpi-badge.danger,.metric-level.danger {{ background:var(--danger-light); color:var(--danger); }}
.detail-row {{ display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:20px; }}
.main-card,.rules-panel {{ padding:24px; }}
.card-header,.rp-header {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:18px; }}
.card-title,.rp-title {{ font-size:15px; font-weight:800; }}
.report-section {{ margin-top:22px; padding-top:20px; border-top:1px solid var(--line-soft); }}
.report-section:first-of-type {{ margin-top:0; padding-top:0; border-top:0; }}
.report-section-title {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }}
.report-section-title h2 {{ margin:0; font-size:15px; font-weight:800; }}
.report-section-title span {{ color:var(--soft); font-size:12px; }}
.section-tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }}
.section-tab {{ padding:10px 14px; border-radius:var(--radius-sm); color:var(--muted); background:var(--line-soft); font-weight:800; font-size:13px; }}
.section-tab.active {{ background:var(--primary); color:#fff; box-shadow:0 8px 18px rgba(26,115,232,.18); }}
.metric-hint {{ display:flex; gap:8px; margin-bottom:12px; color:var(--muted); background:var(--primary-light); border:1px solid rgba(26,115,232,.12); border-radius:var(--radius-sm); padding:10px 12px; font-size:12px; }}
.metric-hint strong {{ color:var(--primary); }}
.metric-grid {{ display:flex; flex-direction:column; gap:12px; }}
.metric-details {{ border:1px solid transparent; border-radius:var(--radius); background:var(--surface-2); overflow:hidden; }}
.metric-details[open] {{ border-color:var(--line); background:#fff; box-shadow:0 10px 28px rgba(17,24,39,.05); }}
.metric-summary {{ list-style:none; cursor:pointer; }}
.metric-summary::-webkit-details-marker {{ display:none; }}
.metric-row {{ display:flex; align-items:center; gap:16px; padding:16px; }}
.metric-indicator {{ width:4px; height:40px; border-radius:2px; flex-shrink:0; }}
.metric-indicator.good {{ background:var(--primary); }} .metric-indicator.success {{ background:var(--success); }} .metric-indicator.warn {{ background:var(--warning); }} .metric-indicator.danger {{ background:var(--danger); }}
.metric-info {{ flex:1; min-width:0; }} .metric-name {{ font-size:14px; font-weight:800; }} .metric-desc {{ font-size:12px; color:var(--muted); margin-top:2px; }}
.metric-score {{ text-align:right; flex-shrink:0; }} .metric-val {{ font-size:20px; font-weight:800; font-family:Consolas,monospace; }}
.metric-expand {{ width:28px; height:28px; display:grid; place-items:center; border-radius:999px; color:var(--muted); background:#eef2f7; font-size:18px; font-weight:800; }}
.metric-details[open] .metric-expand {{ transform:rotate(45deg); background:var(--primary-light); color:var(--primary); }}
.metric-detail-body {{ border-top:1px solid var(--line-soft); padding:16px 18px 18px 40px; display:grid; gap:14px; }}
.metric-detail-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
.metric-detail-chip {{ background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--radius-sm); padding:10px 12px; }}
.metric-detail-chip span {{ display:block; font-size:11px; font-weight:800; color:var(--soft); margin-bottom:4px; }} .metric-detail-chip strong {{ display:block; font-size:13px; }}
.metric-detail-body p {{ margin:0; color:var(--muted); line-height:1.7; font-size:13px; }} .metric-detail-body p strong {{ color:var(--ink); }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }} th,td {{ padding:8px 10px; border-bottom:1px solid var(--line-soft); text-align:left; }} th {{ background:var(--surface-2); color:var(--soft); }}
.aux-bar {{ width:100%; height:1px; background:var(--line-soft); margin:16px 0 12px; }}
.aux-title {{ font-size:12px; font-weight:800; color:var(--soft); }}
.aux-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:10px; }}
.aux-item {{ padding:12px; border-radius:var(--radius-sm); background:var(--surface-2); text-align:center; }} .aux-val {{ font-size:18px; font-weight:800; font-family:Consolas,monospace; }} .aux-lbl {{ font-size:11px; color:var(--soft); margin-top:4px; }}
.chart-panel {{ margin-top:16px; overflow-x:auto; }} .chart-panel svg {{ min-width:1600px; width:1800px; max-width:none; height:auto; display:block; }}
.rp-count.clear {{ background:var(--success-light); color:var(--success); }} .rp-count.alert {{ background:var(--warning-light); color:var(--warning-text); }} .rp-count.danger {{ background:var(--danger-light); color:var(--danger); }}
.rule-summary-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:14px; }} .rule-summary-card {{ background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--radius-sm); padding:10px; text-align:center; }} .rule-summary-card strong {{ display:block; font-family:Consolas,monospace; font-size:20px; }} .rule-summary-card span {{ display:block; margin-top:4px; color:var(--soft); font-size:11px; }}
.rule-list {{ display:flex; flex-direction:column; gap:10px; }} .rule-item {{ display:flex; flex-direction:column; gap:8px; padding:12px; border:1px solid var(--line-soft); border-radius:var(--radius-sm); background:var(--surface-2); }} .rule-item.hit {{ background:#fff8f7; border-color:#ffd1cb; }} .rule-item.ok {{ background:#f7fbf8; border-color:#d8efde; }}
.rule-code {{ display:flex; align-items:center; justify-content:space-between; gap:8px; font-size:12px; font-weight:800; }} .rule-code code {{ color:var(--soft); }} .rule-code em {{ font-style:normal; color:var(--danger); background:var(--danger-light); border-radius:999px; padding:3px 8px; font-size:10px; }}
.rule-desc {{ color:var(--muted); line-height:1.65; font-size:12px; }} .rule-status {{ display:flex; align-items:center; gap:6px; font-size:11px; font-weight:800; }} .rule-status span {{ width:6px; height:6px; border-radius:50%; }} .rule-status.pass {{ color:var(--success); }} .rule-status.pass span {{ background:var(--success); }} .rule-status.fail {{ color:var(--danger); }} .rule-status.fail span {{ background:var(--danger); }}
@media (max-width:1200px) {{ .detail-row {{ grid-template-columns:1fr; }} .kpi-row,.aux-grid {{ grid-template-columns:repeat(2,1fr); }} }}
@media (max-width:760px) {{ .kpi-row,.aux-grid,.metric-detail-grid {{ grid-template-columns:1fr; }} .main {{ padding:12px; }} }}
</style>
</head>
<body>
<main class="main">
  <div class="main-card">
    <section class="report-section">
      <div class="report-section-title"><h2>导入识别</h2><span>文件结构与字段识别</span></div>
      <table><tr><th>文件</th><td>{_escape(profile.get('file_name'))}</td><th>类型</th><td>{_escape(profile.get('file_type'))}</td></tr><tr><th>原始行/列</th><td>{_escape(profile.get('raw_rows'))}/{_escape(profile.get('raw_columns'))}</td><th>有效/无效</th><td>{_escape(profile.get('valid_rows'))}/{_escape(profile.get('invalid_rows'))}</td></tr><tr><th>站点</th><th>时间来源</th><th>压力列</th><th>有效样本</th><th>时间范围</th></tr>{block_rows}</table>
    </section>
  </div>
  <div class="page-header">
    <div><h1 class="page-title">诊断概览</h1><div class="page-meta">{_escape(result.get('source_filename') or '')} · {_escape(features.get('start'))} – {_escape(features.get('end'))} · {_escape(features.get('count'))} 样本</div></div>
  </div>
  <div class="kpi-row">
    <div class="kpi-card"><div class="kpi-label">健康等级</div><div class="kpi-value-row"><div class="health-badge {health_class}">{_escape(health.get('label'))}</div><div class="kpi-value">{_num(float(health.get('risk_score') or 0), 2)}</div></div><div class="kpi-sub">综合风险分 · 等级 {health_level}/5</div></div>
    <div class="kpi-card"><div class="kpi-label">稳压性能 P01</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p01.get('value') or 0), 2)}%</div><div class="kpi-badge {level_class(p01.get('level'))}">{_escape(p01.get('label'))}</div></div><div class="kpi-sub">实际AC / 出厂AC</div></div>
    <div class="kpi-card"><div class="kpi-label">关闭压力 P02</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p02.get('value') or 0), 2)}%</div><div class="kpi-badge {level_class(p02.get('level'))}">{_escape(p02.get('label'))}</div></div><div class="kpi-sub">实际SG / 出厂SG</div></div>
    <div class="kpi-card"><div class="kpi-label">阀座密封 P03</div><div class="kpi-value-row"><div class="kpi-value">{_num(float(p03.get('value') or 0), 1 if p03.get('unit') == '%' else 2)}{_escape('%' if p03.get('unit') == '%' else '')}</div><div class="kpi-badge {level_class(p03.get('level'))}">{_escape(p03.get('label'))}</div></div><div class="kpi-sub">{_escape('密封爬升指数，非实测泄漏量' if p03.get('unit') == '%' else '实测/导入泄漏量')}</div></div>
  </div>
  <div class="detail-row">
    <div class="main-card">
      <div class="card-header"><span class="card-title">诊断明细</span><span class="card-meta">站点: {_escape(station)} · {_escape(features.get('start'))}</span></div>
      <section class="report-section">
        <div class="report-section-title"><h2>核心性能指标详情</h2><span>P01 / P02 / P03</span></div>
        <div class="metric-hint"><strong>可展开</strong><span>点击每个核心指标右侧的 +，查看计算过程、判定占比和校核证据。</span></div>
        <div class="metric-grid">{''.join(metric_card(item) for item in items)}</div>
        <div class="aux-bar"></div>
        <span class="aux-title">辅助参考指标</span>
        <div class="aux-grid">
          <div class="aux-item"><div class="aux-val">{_num(features.get('mean'), 2)}</div><div class="aux-lbl">均值 kPa</div></div>
          <div class="aux-item"><div class="aux-val">{_num(features.get('std'), 3)}</div><div class="aux-lbl">标准差 σ</div></div>
          <div class="aux-item"><div class="aux-val">{_num(aux_cv, 1)}%</div><div class="aux-lbl">变异系数 CV</div></div>
          <div class="aux-item"><div class="aux-val">{_escape(features.get('count'))}</div><div class="aux-lbl">数据点数</div></div>
        </div>
        <div class="chart-panel">{sparkline_svg(data, result)}</div>
      </section>
      <section class="report-section">
        <div class="report-section-title"><h2>辅助证据</h2><span>规则 / IF / KNN / 基线 / 趋势</span></div>
        <table><tr><th>规则等级</th><td>{_escape(ev.get('rule_level'))}</td><th>综合风险分</th><td>{_num(float(health.get('risk_score') or 0), 4)}</td></tr><tr><th>Isolation Forest</th><td>{_num(float(iso.get('score') or 0), 4)} / {_escape(iso.get('band_label'))}{(' / P' + _escape(iso.get('percentile'))) if iso.get('percentile') is not None else ''}</td><th>KNN</th><td>{_num(float(knn.get('score') or 0), 4)} / {_escape(knn.get('band_label'))}{(' / P' + _escape(knn.get('percentile'))) if knn.get('percentile') is not None else ''}</td></tr><tr><th>基线等级</th><td>{_escape(ev.get('baseline_level'))}</td><th>趋势等级</th><td>{_escape(ev.get('trend_level'))}</td></tr></table>
        <table><tr><th>说明</th></tr>{decision_rows}</table>
      </section>
    </div>
    <div class="rules-panel">
      <div class="rp-header"><span class="rp-title">规则触发</span><span class="rp-count {'clear' if not findings else ('alert' if len(findings) <= 2 else 'danger')}">{len(findings)} 条触发</span></div>
      <div class="rule-summary-grid"><div class="rule-summary-card"><strong>{len(findings)}</strong><span>触发规则</span></div><div class="rule-summary-card"><strong>{max_severity or '-'}</strong><span>最高等级</span></div><div class="rule-summary-card"><strong>{max(0, 8 - len(findings))}</strong><span>未触发规则</span></div></div>
      <div class="rule-list">{''.join(rule_rows)}</div>
    </div>
  </div>
</main>
</body>
</html>"""
    html_path.write_text(html_doc, encoding="utf-8")
    return str(html_path)


def write_reports(result: dict, data: pd.DataFrame, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    station = result["features"]["station"]
    safe_station = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in station)
    json_path = output_dir / f"{safe_station}_diagnosis.json"
    md_path = output_dir / f"{safe_station}_diagnosis.md"
    html_path = output_dir / f"{safe_station}_diagnosis.html"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    features = result["features"]
    health = result["health"]
    findings = result.get("findings", [])
    svg = sparkline_svg(data, result)
    conclusion = _level_summary(int(health["level"]), str(health["label"]), findings)
    maintenance = _maintenance_summary(health, findings)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_files = features.get("source_files", [])
    source_text = "；".join(source_files[:5])
    if len(source_files) > 5:
        source_text += f"；等 {len(source_files)} 个来源"
    isolation = (result.get("ai", {}) or {}).get("isolation_forest", {}) or {}
    knn = (result.get("ai", {}) or {}).get("knn", {}) or {}
    performance = result.get("performance", {}) or {}
    perf_params = performance.get("params", {}) or {}
    perf_overall = performance.get("overall") or {}
    perf_summary = _performance_summary_text(performance)
    performance_cards = (
        _performance_card_html(_performance_item(performance, "P01"), "调压器稳压性能")
        + _performance_card_html(_performance_item(performance, "P02"), "调压器关闭压力性能")
        + _performance_card_html(_performance_item(performance, "P03"), "调压器阀座密封性能")
    )
    model_method = _model_method_label(isolation)
    model_band = isolation.get("band_label", "")
    if isolation.get("percentile") is not None:
        model_band = f"{model_band} / P{isolation.get('percentile')}"
    knn_band = knn.get("band_label", "")
    if knn.get("percentile") is not None:
        knn_band = f"{knn_band} / P{knn.get('percentile')}"

    findings_table = _findings_md(findings)
    performance_table = _performance_md(performance)
    performance_evidence_table = _performance_evidence_md(performance)
    baseline_table = _baseline_md(result.get("ai", {}))
    decision_table = _decision_reasons_md(health)

    md = f"""# {station} 调压器健康诊断报告

## 一、报告信息

| 项目 | 内容 |
|---|---|
| 站点名称 | {station} |
| 报告生成时间 | {generated_at} |
| 数据时间范围 | {features['start']} 至 {features['end']} |
| 样本数量 | {features['count']} |
| 数据来源 | {source_text} |

## 二、诊断结论

| 项目 | 结果 |
|---|---|
| 健康等级 | {health['level']}级（{health['label']}） |
| 三项性能综合 | {perf_overall.get('level', '')}级（{perf_overall.get('label', '')}） |
| 三项性能明细 | {perf_summary} |
| 结论摘要 | {conclusion} |
| 处置建议 | {maintenance} |

## 三、核心性能指标评价

| 参数 | 数值 |
|---|---:|
| 出口压力设定值 | {perf_params.get('set_pressure_kpa', '')} KPa |
| 稳压精度等级 AC | {perf_params.get('ac_percent', '')}% |
| AC换算压力限值 | {perf_params.get('ac_limit_kpa', '')} KPa |
| 关闭压力等级 SG | {perf_params.get('sg_percent', '')}% |
| SG换算压力限值 | {perf_params.get('sg_limit_kpa', '')} KPa |
| 阀座泄漏量限值 | {perf_params.get('seat_leak_limit', '')} {perf_params.get('seat_leak_unit', '')} |
| 压力采样估算限值 | {perf_params.get('seat_leak_surrogate_limit_kpa', '')} KPa |

{performance_table}

### 稳健校核证据

{performance_evidence_table}

说明：{performance.get('note', '')}

## 四、判定依据

{decision_table}

## 五、压力运行支持数据

| 指标 | 数值 |
|---|---:|
| 平均压力 | {_num(features.get('mean'))} KPa |
| 最大压力 | {_num(features.get('max'))} KPa |
| 最小压力 | {_num(features.get('min'))} KPa |
| 标准差 | {_num(features.get('std'))} KPa |
| 超过 2.75 KPa 比例 | {_pct(features.get('high_275_ratio'))} |
| 超过 3.0 KPa 比例 | {_pct(features.get('high_300_ratio'))} |
| 低于 2.0 KPa 比例 | {_pct(features.get('low_200_ratio'))} |
| 夜间最大压力 | {_num(features.get('night_max'))} KPa |
| 波动次数 | {features.get('wave_count')} |
| 典型波动间隔 | {_num(features.get('wave_interval_min'))} 分钟 |
| 压力趋势斜率 | {_num(features.get('slope_per_day'), 6)} KPa/日 |

## 六、辅助参考：规则与IF/KNN辅助证据

### 规则触发
{findings_table}

### IF/KNN与基线参考

| 指标 | 结果 |
|---|---|
| 综合风险分 | {health['risk_score']} |
| 异常分 | {health['ai_score']} |
| IF检测方法 | {model_method} |
| Isolation Forest异常分 | {isolation.get('score', '')} |
| Isolation Forest异常区间 | {model_band} |
| KNN异常分 | {knn.get('score', '')} |
| KNN异常区间 | {knn_band} |
| 判定模式 | {health.get('diagnosis_mode', '组合判定')} |

### 基线偏离分析

{baseline_table}

## 七、压力曲线

{svg}

## 八、说明

本报告以技术要求中的调压器稳压性能、调压器关闭压力性能、调压器阀座密封性能为核心判定依据。历史健康基线、异常检测模型、压力规则和趋势分析作为辅助参考；涉及现场维修、部件更换或安全处置时，应结合现场检测、设备设定参数和专业人员复核结果确认。
"""
    md_path.write_text(md, encoding="utf-8")

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(station)} 调压器健康诊断报告</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
:root {{
  color-scheme: light;
  --bg: #F7FAFF;
  --surface: #FFFFFF;
  --ink: #111827;
  --ink-2: #374151;
  --muted: #6B7280;
  --soft: #9CA3AF;
  --line: #E5E7EB;
  --line-soft: #F3F4F6;
  --primary: #1A73E8;
  --primary-light: #EEF4FF;
  --success: #34A853;
  --success-light: #E8F5E9;
  --warning: #FBBC04;
  --warning-light: #FFF8EB;
  --warning-text: #E5A100;
  --danger: #EA4335;
  --danger-light: #FDE8E8;
  --font: "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono: "SF Mono", "Cascadia Code", "Consolas", "Monaco", monospace;
  --radius: 12px;
  --radius-lg: 16px;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: var(--font);
  background: var(--bg);
  color: var(--ink);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}}
.topnav {{
  height: 64px; background: var(--surface);
  border-bottom: 1px solid var(--line);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; position: sticky; top: 0; z-index: 100;
}}
.nav-left {{ display: flex; align-items: center; gap: 14px; }}
.logo {{
  width: 34px; height: 34px; border-radius: 10px;
  background: var(--primary); display: grid; place-items: center;
  color: #fff; font-weight: 700; font-size: 16px;
}}
.app-title {{ font-size: 18px; font-weight: 600; color: var(--ink); }}
.nav-right {{ display: flex; align-items: center; gap: 12px; }}
.nav-pill {{
  padding: 8px 16px; border-radius: 999px;
  background: var(--primary-light); color: var(--primary);
  font-size: 13px; font-weight: 500; text-decoration: none;
}}
.nav-pill:hover {{ background: #dce8fc; }}

.page {{ max-width: 1120px; margin: 0 auto; padding: 28px 24px; }}
.header-card {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg); padding: 28px; margin-bottom: 20px;
}}
.header-card h1 {{ font-size: 24px; font-weight: 600; color: var(--ink); letter-spacing: -0.01em; }}
.header-card .meta {{ margin-top: 8px; color: var(--muted); font-size: 14px; }}
.header-card .badges {{ margin-top: 16px; display: flex; gap: 10px; flex-wrap: wrap; }}
.badge {{
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 999px;
  font-size: 13px; font-weight: 600;
}}
.badge .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.badge.l1 {{ background: var(--primary-light); color: var(--primary); }}
.badge.l1 .dot {{ background: var(--primary); }}
.badge.l2 {{ background: var(--success-light); color: var(--success); }}
.badge.l2 .dot {{ background: var(--success); }}
.badge.l3 {{ background: var(--warning-light); color: var(--warning-text); }}
.badge.l3 .dot {{ background: var(--warning); }}
.badge.l4, .badge.l5 {{ background: var(--danger-light); color: var(--danger); }}
.badge.l4 .dot, .badge.l5 .dot {{ background: var(--danger); }}

section {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg); padding: 24px; margin-bottom: 20px;
}}
section h2 {{ font-size: 17px; font-weight: 600; color: var(--ink); margin-bottom: 16px; }}
section h3 {{ font-size: 14px; font-weight: 600; color: var(--ink-2); margin: 16px 0 10px; }}
p {{ margin: 6px 0; color: var(--ink-2); font-size: 14px; line-height: 1.7; }}
p strong {{ color: var(--ink); font-weight: 600; }}

.kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }}
.kpi {{
  border-radius: var(--radius); padding: 18px; background: var(--surface-2);
  border: 1px solid var(--line);
}}
.kpi.primary {{
  background: linear-gradient(135deg, var(--primary), #1557B0);
  border: none; color: #fff;
}}
.kpi.primary .k {{ color: rgba(255,255,255,.85); }}
.kpi.primary .v {{ color: #fff; }}
.kpi.primary .sub {{ color: rgba(255,255,255,.85); }}
.kpi .k {{ font-size: 12px; font-weight: 500; color: var(--soft); text-transform: uppercase; letter-spacing: 0.03em; }}
.kpi .v {{ font-size: 24px; font-weight: 700; color: var(--ink); margin-top: 6px; letter-spacing: -0.02em; }}
.kpi .sub {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
.kpi .v.l1 {{ color: var(--primary); }}
.kpi .v.l2 {{ color: var(--success); }}
.kpi .v.l3 {{ color: var(--warning-text); }}
.kpi .v.l4, .kpi .v.l5 {{ color: var(--danger); }}
.kpi .v-extra {{ display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; margin-left: 8px; vertical-align: middle; }}
.kpi .v-extra.l1 {{ background: var(--primary-light); color: var(--primary); }}
.kpi .v-extra.l2 {{ background: var(--success-light); color: var(--success); }}
.kpi .v-extra.l3 {{ background: var(--warning-light); color: var(--warning-text); }}
.kpi .v-extra.l4, .kpi .v-extra.l5 {{ background: var(--danger-light); color: var(--danger); }}

table {{
  width: 100%; border-collapse: collapse; font-size: 13px;
}}
th, td {{
  border-bottom: 1px solid var(--line-soft); text-align: left;
  padding: 10px 12px; vertical-align: top;
}}
th {{ color: var(--soft); font-weight: 600; font-size: 12px; background: var(--line-soft); }}
tr:last-child th, tr:last-child td {{ border-bottom: 0; }}

.metric {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 10px; background: var(--surface-2); }}
.metric-name {{ font-size: 14px; font-weight: 600; color: var(--ink); }}
.metric-desc {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.metric-value {{ text-align: right; }}
.metric-value .val {{ font-size: 20px; font-weight: 700; font-family: var(--font-mono); color: var(--ink); }}
.metric-value .level {{ display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; margin-top: 4px; }}
.metric-value .level.l1 {{ background: var(--primary-light); color: var(--primary); }}
.metric-value .level.l2 {{ background: var(--success-light); color: var(--success); }}
.metric-value .level.l3 {{ background: var(--warning-light); color: var(--warning-text); }}
.metric-value .level.l4, .metric-value .level.l5 {{ background: var(--danger-light); color: var(--danger); }}

.chart {{
  border: 1px solid var(--line); border-radius: var(--radius);
  padding: 16px; background: #fff; overflow-x: auto;
}}
.chart svg {{ display: block; max-width: 100%; height: auto; }}
.muted {{ color: var(--muted); font-size: 13px; }}

@media (max-width: 900px) {{
  .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .page {{ padding: 20px 16px; }}
}}
@media (max-width: 560px) {{
  .kpi-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<nav class="topnav">
  <div class="nav-left">
    <div class="logo">G</div>
    <span class="app-title">燃气调压器健康诊断系统 · 诊断报告</span>
  </div>
  <div class="nav-right">
    <a class="nav-pill" href="/">← 返回诊断</a>
  </div>
</nav>

<div class="page">
  <div class="header-card">
    <h1>{_escape(station)} 调压器健康诊断报告</h1>
    <div class="meta">生成时间：{_escape(generated_at)}　数据范围：{_escape(features['start'])} 至 {_escape(features['end'])}　样本数：{_escape(features['count'])}</div>
    <div class="badges">
      <div class="badge l{health['level']}"><span class="dot"></span>最终健康 {health['level']}级 {_escape(health['label'])}</div>
      <div class="badge" style="background:var(--primary-light);color:var(--primary)"><span class="dot" style="background:var(--primary)"></span>综合风险分 {_escape(_num(health['risk_score'], 4))}</div>
    </div>
  </div>

  <section>
    <h2>诊断结论</h2>
    <p><strong>结论摘要：</strong>{_escape(conclusion)}</p>
    <p><strong>处置建议：</strong>{_escape(maintenance)}</p>
  </section>

  <section>
    <h2>核心性能指标</h2>
    <div class="kpi-grid">
      <div class="kpi primary">
        <div class="k">最终健康等级</div>
        <div class="v">{health['level']}级 {_escape(health['label'])}</div>
        <div class="sub">综合风险分 {_escape(_num(health['risk_score'], 4))}</div>
      </div>
      <div class="kpi">
        <div class="k">稳压性能 P01</div>
        <div class="v l{_performance_item(performance, 'P01').get('level', 1)}">{_performance_item(performance, 'P01').get('level', 1)}级 {_escape(_performance_item(performance, 'P01').get('label', ''))}</div>
        <div class="sub">{_escape(_performance_item(performance, 'P01').get('name', ''))}</div>
      </div>
      <div class="kpi">
        <div class="k">关闭压力 P02</div>
        <div class="v l{_performance_item(performance, 'P02').get('level', 1)}">{_performance_item(performance, 'P02').get('level', 1)}级 {_escape(_performance_item(performance, 'P02').get('label', ''))}</div>
        <div class="sub">{_escape(_performance_item(performance, 'P02').get('name', ''))}</div>
      </div>
      <div class="kpi">
        <div class="k">阀座密封 P03</div>
        <div class="v l{_performance_item(performance, 'P03').get('level', 1)}">{_performance_item(performance, 'P03').get('level', 1)}级 {_escape(_performance_item(performance, 'P03').get('label', ''))}</div>
        <div class="sub">{_escape(_performance_item(performance, 'P03').get('name', ''))}</div>
      </div>
    </div>
    <h3>稳健校核证据</h3>
    {_html_table_from_markdown_table(performance_evidence_table)}
    <p class="muted">{_escape(performance.get('note', ''))}</p>
  </section>

  <section>
    <h2>判定参数</h2>
    <table>
      <tr><th>出口压力设定值</th><td>{_escape(perf_params.get('set_pressure_kpa', ''))} KPa</td><th>AC / SG</th><td>{_escape(perf_params.get('ac_percent', ''))}% / {_escape(perf_params.get('sg_percent', ''))}%</td></tr>
      <tr><th>AC / SG换算限值</th><td>{_escape(perf_params.get('ac_limit_kpa', ''))} / {_escape(perf_params.get('sg_limit_kpa', ''))} KPa</td><th>阀座泄漏量限值</th><td>{_escape(perf_params.get('seat_leak_limit', ''))} {_escape(perf_params.get('seat_leak_unit', ''))}</td></tr>
    </table>
    {_html_table_from_markdown_table(performance_table)}
  </section>

  <section>
    <h2>判定依据</h2>
    <table>
      <tr><th>证据等级</th><td colspan="3">规则{_escape((health.get('evidence') or {}).get('rule_level', ''))} / IF{_escape((health.get('evidence') or {}).get('isolation_level', ''))} / KNN{_escape((health.get('evidence') or {}).get('knn_level', ''))} / 基线{_escape((health.get('evidence') or {}).get('baseline_level', ''))} / 趋势{_escape((health.get('evidence') or {}).get('trend_level', ''))}</td></tr>
      <tr><th>IF/KNN一致性</th><td>{'一致异常' if (health.get('evidence') or {}).get('model_consensus') else '未形成一致异常'}</td><th>强证据/明显证据</th><td>{_escape((health.get('evidence') or {}).get('strong_evidence_count', ''))} / {_escape((health.get('evidence') or {}).get('obvious_evidence_count', ''))}</td></tr>
    </table>
    {_html_table_from_markdown_table(decision_table)}
  </section>

  <section>
    <h2>压力运行支持数据</h2>
    <div class="kpi-grid">
      <div class="kpi"><div class="k">平均压力</div><div class="v">{_num(features.get('mean'))}</div><div class="sub">KPa</div></div>
      <div class="kpi"><div class="k">最大压力</div><div class="v">{_num(features.get('max'))}</div><div class="sub">KPa</div></div>
      <div class="kpi"><div class="k">最小压力</div><div class="v">{_num(features.get('min'))}</div><div class="sub">KPa</div></div>
      <div class="kpi"><div class="k">标准差</div><div class="v">{_num(features.get('std'))}</div><div class="sub">KPa</div></div>
      <div class="kpi"><div class="k">超 2.75 比例</div><div class="v">{_pct(features.get('high_275_ratio'))}</div><div class="sub">高压偏移</div></div>
      <div class="kpi"><div class="k">超 3.0 比例</div><div class="v">{_pct(features.get('high_300_ratio'))}</div><div class="sub">超压风险</div></div>
      <div class="kpi"><div class="k">夜间最大</div><div class="v">{_num(features.get('night_max'))}</div><div class="sub">KPa</div></div>
      <div class="kpi"><div class="k">趋势斜率</div><div class="v">{_num(features.get('slope_per_day'), 6)}</div><div class="sub">KPa/日</div></div>
    </div>
  </section>

  <section>
    <h2>辅助参考：规则与IF/KNN辅助证据</h2>
    <p class="muted">以下内容用于解释压力异常来源和数据偏离程度，不替代三项核心性能评价。</p>
    <h3>规则触发</h3>
    {_html_table_from_markdown_table(findings_table)}
    <h3>IF/KNN与基线参考</h3>
    <table>
      <tr><th>综合风险分</th><td>{_escape(health['risk_score'])}</td><th>异常分</th><td>{_escape(health['ai_score'])}</td></tr>
      <tr><th>Isolation Forest</th><td>{_escape(isolation.get('score', ''))}</td><th>异常区间</th><td>{_escape(model_band)}</td></tr>
      <tr><th>KNN异常分</th><td>{_escape(knn.get('score', ''))}</td><th>KNN异常区间</th><td>{_escape(knn_band)}</td></tr>
      <tr><th>判定模式</th><td colspan="3">{_escape(health.get('diagnosis_mode', '组合判定'))}</td></tr>
    </table>
    <h3>基线偏离分析</h3>
    {_html_table_from_markdown_table(baseline_table)}
  </section>

  <section>
    <h2>压力曲线</h2>
    <div class="chart">{svg}</div>
  </section>

  <section>
    <h2>说明</h2>
    <p>本报告以技术要求中的调压器稳压性能、调压器关闭压力性能、调压器阀座密封性能为核心判定依据。历史健康基线、异常检测模型、压力规则和趋势分析作为辅助参考；涉及现场维修、部件更换或安全处置时，应结合现场检测、设备设定参数和专业人员复核结果确认。</p>
  </section>
</div>
</body>
</html>"""
    html_path.write_text(html_doc, encoding="utf-8")
    overview_path = write_main_snapshot_html_report(result, data, output_dir)
    overview_pdf_path = Path(overview_path).with_suffix(".pdf")
    render_html_to_pdf(overview_path, overview_pdf_path)
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "html": str(html_path),
        "overview_html": str(overview_path),
        "pdf": str(overview_pdf_path),
        "overview_pdf": str(overview_pdf_path),
    }
