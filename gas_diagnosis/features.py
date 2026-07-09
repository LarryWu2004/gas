"""Feature extraction for pressure time series."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .config import DEFAULT_THRESHOLDS


def _safe_float(value: float | int | np.floating | None) -> float | None:
    if value is None:
        return None
    try:
        if math.isnan(float(value)):
            return None
    except TypeError:
        return None
    return round(float(value), 6)


def _longest_run_minutes(mask: pd.Series, timestamps: pd.Series) -> float:
    if mask.empty or not bool(mask.any()):
        return 0.0
    ts = pd.to_datetime(timestamps).reset_index(drop=True)
    m = mask.reset_index(drop=True).fillna(False).astype(bool)
    best = 0.0
    start = None
    prev = None
    for i, flag in enumerate(m):
        current = ts.iloc[i]
        if flag and start is None:
            start = current
        if not flag and start is not None:
            best = max(best, (prev - start).total_seconds() / 60.0 if prev is not None else 0.0)
            start = None
        prev = current
    if start is not None and prev is not None:
        best = max(best, (prev - start).total_seconds() / 60.0)
    return round(float(best), 3)


def _linear_slope_per_day(timestamps: pd.Series, values: pd.Series) -> float:
    if len(values) < 3:
        return 0.0
    ts = pd.to_datetime(timestamps)
    x = (ts - ts.min()).dt.total_seconds().to_numpy(dtype=float) / 86400.0
    y = values.to_numpy(dtype=float)
    if np.nanmax(x) == np.nanmin(x):
        return 0.0
    valid = np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 3:
        return 0.0
    slope, _ = np.polyfit(x[valid], y[valid], 1)
    return round(float(slope), 6)


def _wave_stats(values: pd.Series, timestamps: pd.Series, min_amplitude: float) -> tuple[int, float | None]:
    y = values.to_numpy(dtype=float)
    if len(y) < 5:
        return 0, None
    smooth = pd.Series(y).rolling(window=3, center=True, min_periods=1).mean().to_numpy()
    peaks = []
    troughs = []
    for i in range(1, len(smooth) - 1):
        if smooth[i] > smooth[i - 1] and smooth[i] > smooth[i + 1]:
            if abs(smooth[i] - np.median(smooth[max(0, i - 3) : i + 4])) >= min_amplitude:
                peaks.append(i)
        if smooth[i] < smooth[i - 1] and smooth[i] < smooth[i + 1]:
            if abs(smooth[i] - np.median(smooth[max(0, i - 3) : i + 4])) >= min_amplitude:
                troughs.append(i)
    turning_points = sorted(peaks + troughs)
    if len(turning_points) < 2:
        return len(turning_points), None
    ts = pd.to_datetime(timestamps).reset_index(drop=True)
    intervals = []
    for left, right in zip(turning_points[:-1], turning_points[1:]):
        intervals.append((ts.iloc[right] - ts.iloc[left]).total_seconds() / 60.0)
    return len(turning_points), round(float(np.median(intervals)), 3) if intervals else None


def extract_features(data: pd.DataFrame, thresholds: dict | None = None) -> dict:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if data.empty:
        raise ValueError("cannot extract features from empty data")
    df = data.sort_values("timestamp").copy()
    pressure = df["pressure_kpa"].astype(float)
    ts = pd.to_datetime(df["timestamp"])
    duration_min = max(0.0, (ts.max() - ts.min()).total_seconds() / 60.0)
    diffs = ts.sort_values().diff().dropna().dt.total_seconds() / 60.0

    hours = ts.dt.hour
    night = (hours >= thresholds["night_start_hour"]) & (hours <= thresholds["night_end_hour"])
    morning = (hours >= thresholds["morning_peak_start_hour"]) & (hours <= thresholds["morning_peak_end_hour"])
    evening = (hours >= thresholds["evening_peak_start_hour"]) & (hours <= thresholds["evening_peak_end_hour"])

    wave_count, wave_interval = _wave_stats(
        pressure,
        ts,
        min_amplitude=float(thresholds["wave_min_amplitude_kpa"]),
    )

    high_275 = pressure >= thresholds["high_warning_kpa"]
    high_300 = pressure >= thresholds["high_near_release_kpa"]
    high_310 = pressure >= thresholds["high_release_kpa"]
    low_200 = pressure <= thresholds["low_pressure_kpa"]

    features = {
        "station": str(df["station"].iloc[0]),
        "start": ts.min().isoformat(),
        "end": ts.max().isoformat(),
        "count": int(len(df)),
        "duration_hours": _safe_float(duration_min / 60.0),
        "median_interval_min": _safe_float(diffs.median() if not diffs.empty else None),
        "mean": _safe_float(pressure.mean()),
        "median": _safe_float(pressure.median()),
        "std": _safe_float(pressure.std(ddof=0)),
        "min": _safe_float(pressure.min()),
        "max": _safe_float(pressure.max()),
        "p01": _safe_float(pressure.quantile(0.01)),
        "p05": _safe_float(pressure.quantile(0.05)),
        "p95": _safe_float(pressure.quantile(0.95)),
        "p99": _safe_float(pressure.quantile(0.99)),
        "range": _safe_float(pressure.max() - pressure.min()),
        "high_275_ratio": _safe_float(high_275.mean()),
        "high_300_ratio": _safe_float(high_300.mean()),
        "high_310_ratio": _safe_float(high_310.mean()),
        "low_200_ratio": _safe_float(low_200.mean()),
        "longest_high_300_min": _longest_run_minutes(high_300, ts),
        "longest_low_200_min": _longest_run_minutes(low_200, ts),
        "night_mean": _safe_float(pressure[night].mean() if night.any() else None),
        "night_max": _safe_float(pressure[night].max() if night.any() else None),
        "night_high_300_ratio": _safe_float((pressure[night] >= thresholds["high_near_release_kpa"]).mean() if night.any() else 0.0),
        "morning_min": _safe_float(pressure[morning].min() if morning.any() else None),
        "evening_min": _safe_float(pressure[evening].min() if evening.any() else None),
        "peak_low_200_ratio": _safe_float((pressure[morning | evening] <= thresholds["low_pressure_kpa"]).mean() if (morning | evening).any() else 0.0),
        "wave_count": int(wave_count),
        "wave_interval_min": _safe_float(wave_interval),
        "slope_per_day": _linear_slope_per_day(ts, pressure),
        "source_files": sorted(df["source_file"].dropna().astype(str).unique().tolist()),
    }
    return features


def daily_feature_table(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if data.empty:
        return pd.DataFrame()
    df = data.copy()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    for (_, _date), group in df.groupby(["station", "date"], sort=True):
        if len(group) >= 12:
            rows.append(extract_features(group))
    return pd.DataFrame(rows)


def resample_curve(data: pd.DataFrame, points: int = 96) -> list[float]:
    if data.empty:
        return []
    df = data.sort_values("timestamp")
    y = df["pressure_kpa"].astype(float).to_numpy()
    if len(y) == 1:
        return [float(y[0])] * points
    x_old = np.linspace(0.0, 1.0, len(y))
    x_new = np.linspace(0.0, 1.0, points)
    return np.interp(x_new, x_old, y).round(6).tolist()
