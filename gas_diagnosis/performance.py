"""Single-item performance evaluation from the technical requirement."""

from __future__ import annotations

import pandas as pd


DEFAULT_PERFORMANCE_PARAMS = {
    "set_pressure_kpa": 2.5,
    "ac_percent": 10.0,
    "sg_percent": 20.0,
    "extreme_sample_n": 20,
    "inlet_pressure_max_fluctuation_percent": 5.0,
    "seat_leak_limit": 0.20,
    "seat_leak_unit": "同输入单位",
    "seat_leak_surrogate_limit_kpa": 0.50,
}


GRADE_LABELS = {
    1: "优良",
    2: "合格",
    3: "轻度偏差",
    4: "较大偏差",
    5: "严重偏差",
}


def _grade_by_ratio(ratio: float, thresholds: tuple[float, float, float, float]) -> tuple[int, str]:
    if ratio <= thresholds[0]:
        level = 1
    elif ratio <= thresholds[1]:
        level = 2
    elif ratio <= thresholds[2]:
        level = 3
    elif ratio <= thresholds[3]:
        level = 4
    else:
        level = 5
    return level, GRADE_LABELS[level]


def _ratio_risk_score(ratio: float, severe_threshold: float, thresholds: tuple[float, float, float, float] | None = None) -> float:
    if thresholds:
        t1, t2, t3, t4 = thresholds
        if ratio <= t1:
            score = 4.0 + ratio / max(t1, 1e-9) * 14.0
        elif ratio <= t2:
            score = 18.0 + (ratio - t1) / max(t2 - t1, 1e-9) * 12.0
        elif ratio <= t3:
            score = 30.0 + (ratio - t2) / max(t3 - t2, 1e-9) * 25.0
        elif ratio <= t4:
            score = 55.0 + (ratio - t3) / max(t4 - t3, 1e-9) * 23.0
        else:
            score = 78.0 + (ratio - t4) / max(severe_threshold, 1e-9) * 22.0
        return round(min(100.0, max(0.0, score)), 1)
    return round(min(100.0, max(0.0, ratio / max(severe_threshold, 1e-9) * 100.0)), 1)


def _param_float(params: dict, key: str, default: float) -> float:
    value = params.get(key)
    if value in (None, ""):
        return float(default)
    return float(value)


def _resolve_ac_sg_limits(params: dict, set_pressure: float) -> tuple[float, float, float, float]:
    """Return AC%, AC kPa limit, SG%, SG kPa limit.

    AC and SG are industry percentage classes. Legacy absolute kPa inputs are
    still accepted for old reports and tests.
    """
    if params.get("ac_percent") not in (None, ""):
        ac_percent = _param_float(params, "ac_percent", DEFAULT_PERFORMANCE_PARAMS["ac_percent"])
        ac_limit = abs(set_pressure) * ac_percent / 100.0
    else:
        ac_limit = _param_float(params, "ac_kpa", abs(set_pressure) * DEFAULT_PERFORMANCE_PARAMS["ac_percent"] / 100.0)
        ac_percent = ac_limit / max(abs(set_pressure), 1e-9) * 100.0

    if params.get("sg_percent") not in (None, ""):
        sg_percent = _param_float(params, "sg_percent", DEFAULT_PERFORMANCE_PARAMS["sg_percent"])
        sg_limit = abs(set_pressure) * sg_percent / 100.0
    else:
        sg_limit = _param_float(params, "sg_kpa", abs(set_pressure) * DEFAULT_PERFORMANCE_PARAMS["sg_percent"] / 100.0)
        sg_percent = sg_limit / max(abs(set_pressure), 1e-9) * 100.0

    return max(ac_percent, 1e-9), max(ac_limit, 1e-9), max(sg_percent, 1e-9), max(sg_limit, 1e-9)


def _param_int(params: dict, key: str, default: int) -> int:
    value = params.get(key)
    if value in (None, ""):
        return int(default)
    return int(float(value))


def _top_bottom_mean(values: pd.Series, n: int) -> tuple[float, float, int]:
    clean = values.dropna().astype(float).sort_values()
    if clean.empty:
        return 0.0, 0.0, 0
    count = max(1, min(int(n), int(clean.size)))
    pmin = float(clean.head(count).mean())
    pmax = float(clean.tail(count).mean())
    return pmax, pmin, count


def _top_bottom_points(values: pd.Series, n: int, total_count: int) -> dict:
    clean = values.dropna().astype(float).sort_values()
    if clean.empty:
        return {"pmax_points": [], "pmin_points": []}
    count = max(1, min(int(n), int(clean.size)))

    def point_rows(series: pd.Series) -> list[dict]:
        rows = []
        for idx, value in series.items():
            raw_idx = int(idx)
            rows.append(
                {
                    "index": raw_idx,
                    "curve_index": int(round(raw_idx * 179 / max(total_count - 1, 1))),
                    "value": round(float(value), 6),
                }
            )
        return rows

    return {
        "source_count": int(total_count),
        "pmax_points": point_rows(clean.tail(count)),
        "pmin_points": point_rows(clean.head(count)),
    }


def _inlet_summary(data: pd.DataFrame, params: dict) -> dict:
    series = pd.to_numeric(data.get("inlet_pressure_kpa"), errors="coerce") if "inlet_pressure_kpa" in data.columns else pd.Series(dtype=float)
    valid = series.dropna().astype(float)
    max_fluct = _param_float(
        params,
        "inlet_pressure_max_fluctuation_percent",
        DEFAULT_PERFORMANCE_PARAMS["inlet_pressure_max_fluctuation_percent"],
    )
    if valid.empty:
        return {
            "available": False,
            "status": "未导入进口压力字段",
            "mean": None,
            "min": None,
            "max": None,
            "fluctuation_percent": None,
            "fluctuation_ok": None,
            "max_fluctuation_percent": max_fluct,
        }
    mean = float(valid.mean())
    min_v = float(valid.min())
    max_v = float(valid.max())
    fluct = (max_v - min_v) / max(abs(mean), 1e-9) * 100.0
    fluct_ok = fluct <= max_fluct
    return {
        "available": True,
        "status": "进口压力波动满足前提" if fluct_ok else "进口压力波动超过前提",
        "mean": round(mean, 4),
        "min": round(min_v, 4),
        "max": round(max_v, 4),
        "fluctuation_percent": round(fluct, 4),
        "fluctuation_ok": fluct_ok,
        "max_fluctuation_percent": round(max_fluct, 4),
    }


def _longest_run_minutes(mask: pd.Series, ts: pd.Series) -> float:
    if mask.empty or not bool(mask.any()):
        return 0.0
    flags = mask.reset_index(drop=True).fillna(False).astype(bool)
    times = pd.to_datetime(ts).reset_index(drop=True)
    best = 0.0
    start = None
    prev = None
    for i, flag in enumerate(flags):
        current = times.iloc[i]
        if flag and start is None:
            start = current
        if not flag and start is not None:
            best = max(best, (prev - start).total_seconds() / 60.0 if prev is not None else 0.0)
            start = None
        prev = current
    if start is not None and prev is not None:
        best = max(best, (prev - start).total_seconds() / 60.0)
    return round(float(best), 3)


def _slope_per_hour(values: pd.Series, ts: pd.Series) -> float:
    if len(values) < 3:
        return 0.0
    x = (pd.to_datetime(ts) - pd.to_datetime(ts).min()).dt.total_seconds().to_numpy(dtype=float) / 3600.0
    y = values.astype(float).to_numpy()
    valid = pd.Series(x).notna().to_numpy() & pd.Series(y).notna().to_numpy()
    if valid.sum() < 3 or float(x[valid].max()) == float(x[valid].min()):
        return 0.0
    slope = pd.Series(y[valid]).cov(pd.Series(x[valid])) / pd.Series(x[valid]).var()
    return round(float(slope), 6)


def _typical_interval_minutes(ts: pd.Series) -> float:
    times = pd.to_datetime(ts).sort_values()
    diffs = times.diff().dt.total_seconds().dropna() / 60.0
    diffs = diffs[(diffs > 0) & (diffs < 24 * 60)]
    if diffs.empty:
        return 10.0
    return max(1.0, float(diffs.median()))


def _low_flow_event_analysis(pressure: pd.Series, ts: pd.Series, set_pressure: float, ac_limit: float, sg_limit: float) -> dict:
    """Infer lock-up / creep evidence from operating pressure only.

    This is not a replacement for a no-flow lock-up or leakage test. It looks
    for quiet night/low-variation windows, then estimates: (1) lock-up platform
    pressure and short projection, (2) positive pressure creep.
    """
    ordered = pd.DataFrame({"timestamp": pd.to_datetime(ts), "pressure": pressure.astype(float)}).dropna()
    ordered = ordered.sort_values("timestamp").reset_index(drop=True)
    n = len(ordered)
    if n < 8:
        return {
            "candidate_count": 0,
            "window_minutes": 0.0,
            "lockup_pressure": float(pressure.max()) if len(pressure) else set_pressure,
            "lockup_projection": float(pressure.max()) if len(pressure) else set_pressure,
            "positive_slope": 0.0,
            "creep_rise": 0.0,
            "spread": 0.0,
            "source": "样本不足，无法形成低流量片段模拟",
            "evidence_level": "insufficient",
        }

    interval_min = _typical_interval_minutes(ordered["timestamp"])
    window_n = int(round(90.0 / interval_min))
    window_n = max(8, min(window_n, max(8, n // 2), 72))
    if window_n >= n:
        window_n = max(8, n // 2)
    step = max(1, window_n // 4)
    quiet_spread_limit = max(0.70 * ac_limit, 0.06)
    quiet_std_limit = max(0.28 * ac_limit, 0.025)
    windows: list[dict] = []
    fallback_windows: list[dict] = []

    for start in range(0, max(1, n - window_n + 1), step):
        win = ordered.iloc[start : start + window_n]
        if len(win) < 6:
            continue
        values = win["pressure"]
        win_ts = win["timestamp"]
        split = max(2, len(win) // 3)
        early = float(values.head(split).median())
        late = float(values.tail(split).median())
        spread = float(max(0.0, values.quantile(0.95) - values.quantile(0.05)))
        std = float(values.std(ddof=0))
        slope = _slope_per_hour(values, win_ts)
        mid_hour = int(win_ts.iloc[len(win_ts) // 2].hour)
        night_like = 0 <= mid_hour <= 5
        quiet_like = spread <= quiet_spread_limit or std <= quiet_std_limit
        candidate = {
            "start": win_ts.iloc[0].isoformat(),
            "end": win_ts.iloc[-1].isoformat(),
            "p95": float(values.quantile(0.95)),
            "max": float(values.max()),
            "median": float(values.median()),
            "rise": max(0.0, late - early),
            "slope": float(slope),
            "spread": spread,
            "std": std,
            "night_like": night_like,
            "quiet_like": quiet_like,
        }
        fallback_windows.append(candidate)
        if night_like or quiet_like:
            windows.append(candidate)

    if not windows:
        windows = fallback_windows
        evidence_level = "weak"
        source = "未识别到明确夜间/静稳片段，使用全时段滚动窗口弱估算"
    else:
        evidence_level = "estimated"
        source = "夜间或静稳低流量片段模拟"

    if not windows:
        return {
            "candidate_count": 0,
            "window_minutes": round(window_n * interval_min, 2),
            "lockup_pressure": float(ordered["pressure"].quantile(0.99)),
            "lockup_projection": float(ordered["pressure"].quantile(0.99)),
            "positive_slope": 0.0,
            "creep_rise": 0.0,
            "spread": 0.0,
            "source": "无可用窗口",
            "evidence_level": "insufficient",
        }

    max_plateau = max(item["p95"] for item in windows)
    max_positive_slope = max(0.0, max(item["slope"] for item in windows))
    max_rise = max(item["rise"] for item in windows)
    max_spread = max(item["spread"] for item in windows)
    rise_series = pd.Series([float(item["rise"]) for item in windows], dtype=float)
    positive_rise_threshold = max(0.015, 0.08 * ac_limit)
    strong_rise_threshold = max(0.035, 0.18 * ac_limit)
    positive_window_count = int((rise_series >= positive_rise_threshold).sum())
    strong_window_count = int((rise_series >= strong_rise_threshold).sum())
    positive_window_ratio = positive_window_count / max(len(windows), 1)
    strong_window_ratio = strong_window_count / max(len(windows), 1)
    projected_minutes = 30.0
    lockup_projection = max_plateau + max_positive_slope * (projected_minutes / 60.0)
    creep_projection = max(max_rise, max_positive_slope * 1.0)
    display_windows = sorted(
        windows,
        key=lambda item: (
            max(0.0, item["p95"] - (set_pressure + sg_limit)),
            item["rise"],
            max(0.0, item["slope"]),
            item["p95"],
        ),
        reverse=True,
    )[:8]

    return {
        "candidate_count": len(windows),
        "window_minutes": round(window_n * interval_min, 2),
        "lockup_pressure": round(float(max_plateau), 6),
        "lockup_projection": round(float(lockup_projection), 6),
        "positive_slope": round(float(max_positive_slope), 6),
        "creep_rise": round(float(creep_projection), 6),
        "p90_rise": round(float(rise_series.quantile(0.90)), 6),
        "p75_rise": round(float(rise_series.quantile(0.75)), 6),
        "median_rise": round(float(rise_series.median()), 6),
        "positive_rise_threshold": round(float(positive_rise_threshold), 6),
        "strong_rise_threshold": round(float(strong_rise_threshold), 6),
        "positive_window_count": positive_window_count,
        "strong_window_count": strong_window_count,
        "positive_window_ratio": round(float(positive_window_ratio), 6),
        "strong_window_ratio": round(float(strong_window_ratio), 6),
        "spread": round(float(max_spread), 6),
        "source": source,
        "evidence_level": evidence_level,
        "projected_minutes": projected_minutes,
        "display_windows": [
            {
                "start": item["start"],
                "end": item["end"],
                "p95": round(float(item["p95"]), 4),
                "rise": round(float(item["rise"]), 4),
                "slope": round(float(item["slope"]), 6),
            }
            for item in display_windows
        ],
    }


def _confidence(sample_count: int, duration_hours: float, night_count: int | None = None) -> dict:
    score = 0
    reasons = []
    if sample_count >= 100:
        score += 35
    elif sample_count >= 30:
        score += 20
    else:
        reasons.append("样本量偏少")

    if duration_hours >= 12:
        score += 30
    elif duration_hours >= 2:
        score += 18
    else:
        reasons.append("覆盖时长偏短")

    if night_count is None:
        score += 25
    elif night_count >= 30:
        score += 35
    elif night_count >= 8:
        score += 20
    else:
        reasons.append("夜间低流量样本不足")

    if score >= 75:
        label = "高"
    elif score >= 45:
        label = "中"
    else:
        label = "低"
    return {
        "score": min(100, score),
        "label": label,
        "reason": "；".join(reasons) if reasons else "样本覆盖满足当前判断要求",
    }


def _overall_level(items: list[dict]) -> dict:
    levels = []
    surrogate_p03_downgraded = False
    for item in items:
        level = int(item["level"])
        if item.get("code") == "P03" and item.get("confirmation_status") == "pressure_surrogate" and level == 3:
            level = 2
            surrogate_p03_downgraded = True
        levels.append(level)
    if not levels:
        return {"level": 1, "label": "优良", "basis": "无单项偏差"}
    severe = sum(1 for level in levels if level >= 5)
    large = sum(1 for level in levels if level >= 4)
    light = sum(1 for level in levels if level >= 3)
    qualified = sum(1 for level in levels if level == 2)

    if severe >= 1 or large >= 2:
        level = 5
        basis = "存在严重偏差单项，或两个及以上较大偏差单项"
    elif large >= 1 or light > 2:
        level = 4
        basis = "存在较大偏差单项，或轻度偏差单项超过2个"
    elif light <= 2 and light >= 1:
        level = 3
        basis = "不超过2个轻度偏差单项"
    elif qualified >= 1:
        level = 2
        basis = "存在合格单项，且无偏差项"
    else:
        level = 1
        basis = "所有单项均为优良"
    if surrogate_p03_downgraded:
        basis += "；阀座密封未导入正式泄漏量字段，压力采样估算仅作参考校核"
    return {"level": level, "label": GRADE_LABELS[level], "basis": basis}


def evaluate_performance(data: pd.DataFrame, features: dict, params: dict | None = None) -> dict:
    p = {**DEFAULT_PERFORMANCE_PARAMS, **(params or {})}
    pressure = data.sort_values("timestamp")["pressure_kpa"].astype(float)
    ts = pd.to_datetime(data.sort_values("timestamp")["timestamp"])
    set_pressure = float(p["set_pressure_kpa"])
    ac_percent, ac_limit_kpa, sg_percent, sg_limit_kpa = _resolve_ac_sg_limits(p, set_pressure)
    leak_limit = max(_param_float(p, "seat_leak_limit", _param_float(p, "seat_leak_limit_kpa", 0.20)), 1e-9)
    leak_unit = str(p.get("seat_leak_unit") or "同输入单位")
    surrogate_leak_limit_kpa = max(
        _param_float(
            p,
            "seat_leak_surrogate_limit_kpa",
            _param_float(p, "seat_leak_limit_kpa", DEFAULT_PERFORMANCE_PARAMS["seat_leak_surrogate_limit_kpa"]),
        ),
        1e-9,
    )
    duration_hours = max(0.0, (ts.max() - ts.min()).total_seconds() / 3600.0)

    hours = ts.dt.hour
    inlet = _inlet_summary(data, p)
    extreme_n = max(1, _param_int(p, "extreme_sample_n", DEFAULT_PERFORMANCE_PARAMS["extreme_sample_n"]))
    day_mask = (hours >= 6) & (hours <= 22)
    day_pressure = pressure[day_mask]
    if len(day_pressure) >= max(6, min(extreme_n * 2, 20)):
        steady_source = "日间样本"
        steady_sample = day_pressure
        steady_sample_ts = ts[day_mask]
    else:
        steady_source = "日间样本不足，使用全时段样本"
        steady_sample = pressure
        steady_sample_ts = ts
    steady_pmax, steady_pmin, steady_n = _top_bottom_mean(steady_sample, extreme_n)
    steady_extreme_points = _top_bottom_points(steady_sample, extreme_n, len(pressure))
    steady_positive_deviation = max(0.0, steady_pmax - set_pressure)
    steady_negative_deviation = max(0.0, set_pressure - steady_pmin)
    steady_grade_deviation = max(steady_positive_deviation, steady_negative_deviation)
    steady_actual_ac_percent = steady_grade_deviation / max(abs(set_pressure), 1e-9) * 100.0
    steady_span_ac_percent = max(0.0, steady_pmax - steady_pmin) / max(2.0 * abs(set_pressure), 1e-9) * 100.0
    steady_dev_series = (pressure - set_pressure).abs()
    steady_deviation = float(steady_dev_series.max())
    steady_p95 = float(steady_dev_series.quantile(0.95))
    steady_p99 = float(steady_dev_series.quantile(0.99))
    steady_over_ac = steady_dev_series > ac_limit_kpa
    steady_over_08ac = steady_dev_series > 0.8 * ac_limit_kpa
    steady_over_ac_ratio = float(steady_over_ac.mean())
    steady_sustained_over_ac_min = _longest_run_minutes(steady_over_ac, ts)
    steady_ratio = steady_actual_ac_percent / ac_percent
    steady_thresholds = (0.8, 1.0, 1.25, 1.5)
    steady_level, steady_label = _grade_by_ratio(steady_ratio, steady_thresholds)
    steady_confidence = _confidence(int(len(steady_sample)), duration_hours)
    if not inlet.get("available"):
        steady_confidence = {
            **steady_confidence,
            "score": max(0, int(steady_confidence.get("score", 0)) - 8),
            "reason": (steady_confidence.get("reason") or "") + "；未导入进口压力字段，无法校验进口压力前提",
        }
    elif inlet.get("fluctuation_ok") is False:
        steady_confidence = {
            **steady_confidence,
            "score": max(0, int(steady_confidence.get("score", 0)) - 20),
            "reason": (steady_confidence.get("reason") or "") + "；进口压力波动超过5%前提，AC结论需复核",
        }

    night_mask = (hours >= 0) & (hours <= 5)
    night_pressure = pressure[night_mask]
    night_ts = ts[night_mask]
    low_flow_analysis = _low_flow_event_analysis(pressure, ts, set_pressure, ac_limit_kpa, sg_limit_kpa)
    closing_series = pd.to_numeric(data.get("closing_pressure_kpa"), errors="coerce") if "closing_pressure_kpa" in data.columns else pd.Series(dtype=float)
    valid_closing = closing_series.dropna().astype(float)
    if not valid_closing.empty:
        closing_pressure = float(valid_closing.quantile(0.99) if len(valid_closing) >= 30 else valid_closing.max())
        closing_source = "导入数据中的关闭压力字段"
        closing_reference_pressure = float(valid_closing.quantile(0.95))
        closing_window = valid_closing
        closing_window_ts = ts.loc[valid_closing.index] if len(ts) == len(closing_series) else ts
        closing_confirmation_status = "formal_closing_pressure"
    elif night_pressure.empty:
        closing_pressure = float(low_flow_analysis["lockup_projection"])
        closing_source = f"{low_flow_analysis['source']}；锁闭平台叠加{low_flow_analysis.get('projected_minutes', 30):g}分钟升压投影"
        closing_reference_pressure = float(low_flow_analysis["lockup_pressure"])
        closing_window = pressure
        closing_window_ts = ts
        closing_confirmation_status = "pressure_surrogate"
    else:
        closing_pressure = float(low_flow_analysis["lockup_projection"])
        closing_source = f"{low_flow_analysis['source']}；锁闭平台叠加{low_flow_analysis.get('projected_minutes', 30):g}分钟升压投影"
        closing_reference_pressure = float(low_flow_analysis["lockup_pressure"])
        closing_window = night_pressure
        closing_window_ts = night_ts
        closing_confirmation_status = "low_flow_surrogate"
    closing_deviation = max(0.0, closing_pressure - set_pressure)
    closing_p95_deviation = max(0.0, closing_reference_pressure - set_pressure)
    closing_increment_series = (closing_window - set_pressure).clip(lower=0.0)
    closing_over_sg_ratio = float(closing_increment_series.gt(sg_limit_kpa).mean())
    closing_sustained_over_sg_min = _longest_run_minutes(closing_increment_series.gt(sg_limit_kpa), closing_window_ts)
    closing_actual_sg_percent = closing_deviation / max(abs(set_pressure), 1e-9) * 100.0
    closing_ratio = closing_actual_sg_percent / sg_percent
    closing_thresholds = (0.8, 1.0, 1.5, 2.0)
    closing_level, closing_label = _grade_by_ratio(closing_ratio, closing_thresholds)
    closing_confidence = _confidence(int(len(pressure)), duration_hours, int(len(night_pressure)))
    if not inlet.get("available"):
        closing_confidence = {
            **closing_confidence,
            "score": max(0, int(closing_confidence.get("score", 0)) - 8),
            "reason": (closing_confidence.get("reason") or "") + "；未导入进口压力字段，无法校验进口压力前提",
        }
    elif inlet.get("fluctuation_ok") is False:
        closing_confidence = {
            **closing_confidence,
            "score": max(0, int(closing_confidence.get("score", 0)) - 20),
            "reason": (closing_confidence.get("reason") or "") + "；进口压力波动超过5%前提，SG结论需复核",
        }
    if closing_confirmation_status != "formal_closing_pressure":
        confidence_penalty = 18 if low_flow_analysis.get("evidence_level") in {"weak", "insufficient"} else 10
        closing_confidence = {
            **closing_confidence,
            "score": max(0, int(closing_confidence.get("score", 0)) - confidence_penalty),
            "reason": (closing_confidence.get("reason") or "") + "；未导入独立关闭压力字段，关闭压力性能为低流量片段模拟估算",
        }

    leakage_series = pd.to_numeric(data.get("seat_leakage"), errors="coerce") if "seat_leakage" in data.columns else pd.Series(dtype=float)
    formal_leakage = bool(leakage_series.notna().any())
    if formal_leakage:
        valid_leakage = leakage_series.dropna().astype(float)
        seat_leak_value = float(valid_leakage.max())
        seat_source = "导入数据中的阀座泄漏量字段"
        seat_slope = 0.0
        seat_window_count = int(valid_leakage.size)
        seat_window_rise = 0.0
        seat_slope_rise = 0.0
        seat_window_spread = 0.0
        seat_limit_for_ratio = leak_limit
        seat_unit = leak_unit
        seat_basis = "按导入泄漏量字段直接判定"
        seat_method = "分级阈值判定 + 实测/导入泄漏量"
        seat_confidence = _confidence(int(valid_leakage.size), duration_hours)
        confirmation_status = "formal_leakage_amount"
    else:
        seat_window_count = int(low_flow_analysis.get("candidate_count") or 0)
        seat_slope = float(low_flow_analysis.get("positive_slope") or 0.0)
        seat_window_rise = float(low_flow_analysis.get("p90_rise") or low_flow_analysis.get("creep_rise") or 0.0)
        seat_slope_rise = max(0.0, seat_slope * 1.0)
        seat_window_spread = float(low_flow_analysis.get("spread") or 0.0)
        positive_window_ratio = float(low_flow_analysis.get("positive_window_ratio") or 0.0)
        strong_window_ratio = float(low_flow_analysis.get("strong_window_ratio") or 0.0)
        seal_reference_kpa = max(0.05, min(surrogate_leak_limit_kpa, max(0.12, 0.55 * ac_limit_kpa)))
        slope_reference_kpa = max(0.04, 0.85 * seal_reference_kpa)
        rise_component = max(0.0, seat_window_rise) / seal_reference_kpa
        slope_component = max(0.0, seat_slope_rise) / slope_reference_kpa
        recurrence_component = max(0.0, (positive_window_ratio - 0.15) / 0.35) + strong_window_ratio * 1.20
        seal_creep_index = 0.55 * rise_component + 0.25 * slope_component + 0.20 * recurrence_component
        if positive_window_ratio >= 0.20:
            seal_creep_index = max(seal_creep_index, 0.75 * rise_component)
        seat_leak_value = float(seal_creep_index * 100.0)
        seat_limit_for_ratio = 100.0
        seat_unit = "%"
        seat_source = f"{low_flow_analysis['source']}中的持续正向爬升"
        seat_basis = f"{seat_source}，当前数据没有泄漏量字段，因此用密封爬升指数作压力侧估算；指数综合P90正向爬升、1小时升压斜率和爬升窗口复现比例，避免只看单个压力峰值"
        seat_method = "低流量/近关闭片段密封爬升指数 + 估算分级；非正式泄漏量检测"
        seat_confidence = _confidence(int(len(pressure)), duration_hours, int(len(night_pressure)))
        confidence_penalty = 20 if low_flow_analysis.get("evidence_level") in {"weak", "insufficient"} else 12
        seat_confidence = {
            **seat_confidence,
            "score": max(0, int(seat_confidence.get("score", 0)) - confidence_penalty),
            "reason": (seat_confidence.get("reason") or "") + "；未导入泄漏量字段，阀座密封为低流量片段模拟估算",
        }
        confirmation_status = "pressure_surrogate"

    seat_ratio = seat_leak_value / seat_limit_for_ratio
    if formal_leakage:
        seat_thresholds = (0.8, 1.0, 5.0, 10.0)
        seat_risk_severe_threshold = 10.0
        seat_reference = f"泄漏限值={seat_limit_for_ratio:.3f}{seat_unit}"
        seat_formula = "正式判定：倍率 = 泄漏量 / 泄漏量限值。"
        seat_threshold_desc = "调压器/切断阀阀座密封：优良≤0.8倍限值；合格≤1倍限值；轻度偏差≤5倍限值；较大偏差≤10倍限值；超过10倍限值为严重偏差。放散阀阀座密封表格限值不同，本系统当前三项核心指标按调压器阀座密封口径展示。"
    else:
        seat_thresholds = (0.80, 1.20, 2.20, 3.50)
        seat_risk_severe_threshold = 3.50
        seat_reference = "密封爬升指数基准=100%"
        seat_formula = (
            "估算判定：密封爬升指数 = 50%×P90正向爬升/动态参考量 + "
            "30%×1小时升压折算/斜率参考量 + 20%×复现比例修正；页面以指数百分比展示。"
        )
        seat_threshold_desc = "压力采样估算：指数≤80%为优良；≤120%为合格；≤220%为轻度偏差；≤350%为较大偏差；超过350%为严重偏差。该口径用于无泄漏量字段时的密封趋势筛查，不等同于正式泄漏量试验。"
    seat_level, seat_label = _grade_by_ratio(seat_ratio, seat_thresholds)

    items = [
        {
            "code": "P01",
            "name": "调压器稳压性能",
            "level": steady_level,
            "label": steady_label,
            "value": round(steady_actual_ac_percent, 4),
            "strict_value": round(steady_grade_deviation, 4),
            "unit": "%",
            "reference": f"出厂AC={ac_percent:g}%",
            "limit_value": round(ac_percent, 4),
            "ratio_formula": f"{steady_actual_ac_percent:.4f}% / {ac_percent:.4f}%",
            "ratio": round(steady_ratio, 4),
            "risk_score": _ratio_risk_score(steady_ratio, 1.5, steady_thresholds),
            "confidence": steady_confidence,
            "definition": "AC为稳压精度等级，表示日间流量变化下出口压力偏离运行设定压力的百分比。",
            "formula": "取日间最高N个压力点均值得Pmax，取日间最低N个压力点均值得Pmin；实际AC = max(Pmax-设定值, 设定值-Pmin, 0) / 设定值 × 100%。",
            "basis": f"{steady_source}；N={steady_n}。前提为进口压力在有效范围内且波动不大于{inlet.get('max_fluctuation_percent')}%。当前前提状态：{inlet.get('status')}。",
            "method": "先由Pmax/Pmin测算实际AC%，再用实际AC% / 出厂AC% 的倍率区间判定：≤0.8为优良，0.8-1为合格，1-1.25为轻度偏差，1.25-1.5为较大偏差，>1.5为严重偏差。",
            "threshold_desc": "优良≤0.8倍出厂AC；合格≤1倍；轻度偏差≤1.25倍；较大偏差≤1.5倍；超过1.5倍为严重偏差。",
            "curve_markers": {
                "source_count": steady_extreme_points.get("source_count", int(len(pressure))),
                "pmax_mean": round(steady_pmax, 4),
                "pmin_mean": round(steady_pmin, 4),
                "pmax_points": steady_extreme_points["pmax_points"],
                "pmin_points": steady_extreme_points["pmin_points"],
            },
            "evidence_metrics": [
                {"name": "极值均值点数N", "value": steady_n, "unit": "点"},
                {"name": "Pmax最高点均值", "value": round(steady_pmax, 4), "unit": "KPa"},
                {"name": "Pmin最低点均值", "value": round(steady_pmin, 4), "unit": "KPa"},
                {"name": "正向偏差", "value": round(steady_positive_deviation, 4), "unit": "KPa"},
                {"name": "负向偏差", "value": round(steady_negative_deviation, 4), "unit": "KPa"},
                {"name": "实际AC", "value": round(steady_actual_ac_percent, 4), "unit": "%"},
                {"name": "半幅AC参考", "value": round(steady_span_ac_percent, 4), "unit": "%"},
                {"name": "出厂AC", "value": round(ac_percent, 4), "unit": "%"},
                {"name": "最大偏差", "value": round(steady_deviation, 4), "unit": "KPa"},
                {"name": "判定偏差", "value": round(steady_grade_deviation, 4), "unit": "KPa"},
                {"name": "P99偏差", "value": round(steady_p99, 4), "unit": "KPa"},
                {"name": "P95偏差", "value": round(steady_p95, 4), "unit": "KPa"},
                {"name": "AC换算限值", "value": round(ac_limit_kpa, 4), "unit": "KPa"},
                {"name": "超过AC比例", "value": round(steady_over_ac_ratio * 100, 2), "unit": "%"},
                {"name": "连续超过AC最长时长", "value": steady_sustained_over_ac_min, "unit": "分钟"},
                {"name": "超过0.8AC比例", "value": round(float(steady_over_08ac.mean()) * 100, 2), "unit": "%"},
                {"name": "进口压力字段状态", "value": inlet.get("status"), "unit": ""},
                {"name": "进口压力波动", "value": "" if inlet.get("fluctuation_percent") is None else inlet.get("fluctuation_percent"), "unit": "" if inlet.get("fluctuation_percent") is None else "%"},
            ],
        },
        {
            "code": "P02",
            "name": "调压器关闭压力性能",
            "level": closing_level,
            "label": closing_label,
            "value": round(closing_actual_sg_percent, 4),
            "strict_value": round(closing_deviation, 4),
            "unit": "%",
            "reference": f"出厂SG={sg_percent:g}%",
            "limit_value": round(sg_percent, 4),
            "ratio_formula": f"{closing_actual_sg_percent:.4f}% / {sg_percent:.4f}%",
            "ratio": round(closing_ratio, 4),
            "risk_score": _ratio_risk_score(closing_ratio, 2.0, closing_thresholds),
            "confidence": closing_confidence,
            "definition": "SG为关闭压力等级，表示关闭或低流量状态下出口压力允许升高量相对于设定压力的百分比。",
            "formula": "实际SG = max(实测关闭压力 - 实测运行压力设定值, 0) / 实测运行压力设定值 × 100%。",
            "basis": f"{closing_source}相对运行压力设定值的正向增量。前提为进口压力在有效范围内且波动不大于{inlet.get('max_fluctuation_percent')}%。当前前提状态：{inlet.get('status')}。",
            "method": "先由实测关闭压力和运行压力设定值测算实际SG%，再用实际SG% / 出厂SG% 的倍率区间判定；无关闭压力字段时仍以低流量窗口锁闭投影作为估算。",
            "threshold_desc": "优良≤0.8倍出厂SG；合格≤1倍；轻度偏差≤1.5倍；较大偏差≤2倍；超过2倍为严重偏差。",
            "measured_pressure": round(closing_pressure, 4),
            "confirmation_status": closing_confirmation_status,
            "evidence_metrics": [
                {"name": "关闭压力估计值", "value": round(closing_pressure, 4), "unit": "KPa"},
                {"name": "运行压力设定值", "value": round(set_pressure, 4), "unit": "KPa"},
                {"name": "关闭压力偏差", "value": round(closing_deviation, 4), "unit": "KPa"},
                {"name": "实际SG", "value": round(closing_actual_sg_percent, 4), "unit": "%"},
                {"name": "出厂SG", "value": round(sg_percent, 4), "unit": "%"},
                {"name": "判定来源", "value": closing_source, "unit": ""},
                {"name": "模拟窗口数量", "value": low_flow_analysis.get("candidate_count", 0), "unit": "个"},
                {"name": "模拟窗口时长", "value": low_flow_analysis.get("window_minutes", 0), "unit": "分钟"},
                {"name": "锁闭平台压力", "value": round(float(low_flow_analysis.get("lockup_pressure", closing_reference_pressure)), 4), "unit": "KPa"},
                {"name": "投影升压斜率", "value": round(float(low_flow_analysis.get("positive_slope", 0.0)), 6), "unit": "KPa/小时"},
                {"name": "夜间/参考P95增量", "value": round(closing_p95_deviation, 4), "unit": "KPa"},
                {"name": "SG换算限值", "value": round(sg_limit_kpa, 4), "unit": "KPa"},
                {"name": "超过SG比例", "value": round(closing_over_sg_ratio * 100, 2), "unit": "%"},
                {"name": "连续超过SG最长时长", "value": closing_sustained_over_sg_min, "unit": "分钟"},
                {"name": "夜间样本数", "value": int(len(night_pressure)), "unit": "点"},
                {"name": "进口压力字段状态", "value": inlet.get("status"), "unit": ""},
                {"name": "进口压力波动", "value": "" if inlet.get("fluctuation_percent") is None else inlet.get("fluctuation_percent"), "unit": "" if inlet.get("fluctuation_percent") is None else "%"},
            ],
        },
        {
            "code": "P03",
            "name": "调压器阀座密封性能",
            "level": seat_level,
            "label": seat_label,
            "value": round(seat_leak_value, 4),
            "strict_value": round(seat_leak_value, 4),
            "unit": seat_unit,
            "reference": seat_reference,
            "limit_value": round(seat_limit_for_ratio, 4),
            "ratio_formula": f"{seat_leak_value:.4f} / {seat_limit_for_ratio:.4f}",
            "ratio": round(seat_ratio, 4),
            "risk_score": _ratio_risk_score(seat_ratio, seat_risk_severe_threshold, seat_thresholds),
            "confidence": seat_confidence,
            "definition": "阀座密封性能正式口径以阀座泄漏量相对限值的倍率判定；若导入数据不含泄漏量字段，则用低流量片段的持续升压特征估算密封趋势。",
            "formula": seat_formula,
            "basis": seat_basis,
            "method": seat_method,
            "threshold_desc": seat_threshold_desc,
            "confirmation_status": confirmation_status,
            "evidence_metrics": [
                {"name": "判定值", "value": round(seat_leak_value, 4), "unit": seat_unit},
                {"name": "判定来源", "value": seat_source, "unit": ""},
                {"name": "泄漏量字段状态", "value": "已导入" if formal_leakage else "未导入", "unit": ""},
                {"name": "密封估算参考量", "value": round(seal_reference_kpa if not formal_leakage else leak_limit, 4), "unit": "KPa" if not formal_leakage else leak_unit},
                {"name": "模拟窗口数量", "value": low_flow_analysis.get("candidate_count", 0), "unit": "个"},
                {"name": "模拟窗口时长", "value": low_flow_analysis.get("window_minutes", 0), "unit": "分钟"},
                {"name": "升压斜率", "value": seat_slope, "unit": "KPa/小时"},
                {"name": "P90正向漂移量", "value": round(seat_window_rise, 4), "unit": "KPa"},
                {"name": "斜率折算升压量", "value": round(seat_slope_rise, 4), "unit": "KPa"},
                {"name": "正向爬升窗口占比", "value": round((positive_window_ratio if not formal_leakage else 0.0) * 100, 2), "unit": "%"},
                {"name": "强爬升窗口占比", "value": round((strong_window_ratio if not formal_leakage else 0.0) * 100, 2), "unit": "%"},
                {"name": "压力波动参考量", "value": round(seat_window_spread, 4), "unit": "KPa"},
                {"name": "参考窗口样本数", "value": seat_window_count, "unit": "点"},
                {"name": "夜间样本数", "value": int(len(night_pressure)), "unit": "点"},
            ],
        },
    ]
    return {
        "params": {
            "set_pressure_kpa": round(set_pressure, 4),
            "ac_percent": round(ac_percent, 4),
            "sg_percent": round(sg_percent, 4),
            "extreme_sample_n": int(extreme_n),
            "inlet_pressure_max_fluctuation_percent": inlet.get("max_fluctuation_percent"),
            "ac_limit_kpa": round(ac_limit_kpa, 4),
            "sg_limit_kpa": round(sg_limit_kpa, 4),
            "seat_leak_limit": round(leak_limit, 4),
            "seat_leak_unit": leak_unit,
            "seat_leak_surrogate_limit_kpa": round(surrogate_leak_limit_kpa, 4),
        },
        "inlet_pressure": inlet,
        "low_flow_analysis": low_flow_analysis,
        "items": items,
        "overall": _overall_level(items),
        "method": "AC按日间最高/最低N点均值测算实际AC%；SG优先按实测关闭压力与运行压力设定值测算实际SG%，无字段时按低流量/近关闭片段锁闭投影估算；阀座密封优先按泄漏量字段判定。",
        "note": "AC、SG判定前提为进口压力在有效范围内且波动不大于5%。未导入进口压力字段时，系统会给出结论但标记为前提未校验；仅有运行压力曲线时，P02/P03为模拟估算证据。",
    }
