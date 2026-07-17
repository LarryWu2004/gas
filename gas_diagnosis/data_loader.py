"""Read and normalize pressure data files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import warnings
from typing import Iterable

import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype


@dataclass(frozen=True)
class LoadResult:
    data: pd.DataFrame
    files_loaded: int
    files_failed: list[str]


def normalize_station_name(name: str) -> str:
    station = Path(str(name)).stem
    station = re.sub(r"^\d{10,}_", "", station)
    station = re.sub(r"\(.*?\)", "", station)
    station = re.sub(r"（.*?）", "", station)
    station = station.replace("桃源", "桃园")
    station = station.replace("澜湾", "蓝湾")
    station = station.replace("二期", "二区")
    station = station.replace("盛秦西苑", "盛秦西苑二区")
    station = station.replace("二区二区", "二区")
    station = station.replace("国兴海澜湾", "国兴蓝海湾")
    station = station.replace("国兴海蓝湾", "国兴蓝海湾")
    station = station.replace("国兴蓝海湾湾", "国兴蓝海湾")
    station = re.sub(r"[-_ ]+$", "", station)
    station = station.strip()
    return station


def station_from_pressure_column(column_name: str, fallback: str) -> str:
    text = str(column_name)
    for marker in ["低压出口", "低压出站", "低压入口", "高压入口", "压力值", "#"]:
        if marker in text:
            text = text.split(marker)[0]
            break
    text = text.strip("_ -")
    generic_names = {
        "压力",
        "出口压力",
        "入口压力",
        "低压出口",
        "低压入口",
        "高压入口",
        "低压出口kpa",
        "低压出口压力",
        "value",
        "value_kpa",
        "measurement",
        "pressure",
        "pressure_kpa",
    }
    if _clean_column_name(text) in generic_names:
        text = fallback
    return normalize_station_name(text or fallback)


def _to_datetime(values: pd.Series) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        if is_datetime64_any_dtype(values):
            return pd.to_datetime(values, errors="coerce")

        if is_numeric_dtype(values):
            numeric = pd.to_numeric(values, errors="coerce")
            valid = numeric.dropna()
            if not valid.empty:
                median = float(valid.median())
                if 20000 <= median <= 80000:
                    return pd.to_datetime(numeric, unit="D", origin="1899-12-30", errors="coerce").dt.round("s")
                if 946684800 <= median <= 4102444800:
                    return pd.to_datetime(numeric, unit="s", errors="coerce").dt.round("s")
                if 946684800000 <= median <= 4102444800000:
                    return pd.to_datetime(numeric, unit="ms", errors="coerce").dt.round("s")

        text = (
            values.astype(str)
            .str.strip()
            .str.replace("年", "-", regex=False)
            .str.replace("月", "-", regex=False)
            .str.replace("日", " ", regex=False)
            .str.replace("/", "-", regex=False)
        )
        return pd.to_datetime(text, errors="coerce")


def _clean_column_name(name: object) -> str:
    return re.sub(r"\s+", "", str(name).strip().lower())


def _name_has_any(name: object, keywords: Iterable[str]) -> bool:
    text = _clean_column_name(name)
    return any(keyword in text for keyword in keywords)


TIME_NAME_KEYWORDS = (
    "时间",
    "日期",
    "时刻",
    "采集",
    "记录",
    "上报",
    "上传",
    "读取",
    "发生",
    "创建",
    "datetime",
    "date",
    "time",
    "timestamp",
)

PRESSURE_NAME_KEYWORDS = (
    "压力",
    "压强",
    "低压",
    "高压",
    "出口",
    "入口",
    "出站",
    "进站",
    "kpa",
    "pressure",
    "press",
)

OUTLET_PRESSURE_NAME_KEYWORDS = (
    "出口",
    "出站",
    "后压",
    "低压出口",
    "低压出站",
    "outlet",
    "downstream",
)

INLET_PRESSURE_NAME_KEYWORDS = (
    "进口",
    "入口",
    "进站",
    "前压",
    "高压入口",
    "inlet",
    "upstream",
)

LEAKAGE_NAME_KEYWORDS = (
    "泄漏",
    "泄露",
    "漏量",
    "漏气",
    "内密封",
    "阀座密封",
    "密封量",
    "leak",
    "leakage",
)

CLOSING_PRESSURE_NAME_KEYWORDS = (
    "关闭压力",
    "关闭压",
    "关阀压力",
    "切断压力",
    "闭锁压力",
    "lockup",
    "lock-up",
    "closingpressure",
    "closing_pressure",
    "shutoff",
    "shut_off",
    "shutoffpressure",
)


def _datetime_quality(ts: pd.Series, valid_mask: pd.Series | None = None) -> tuple[float, float]:
    if valid_mask is not None and valid_mask.any():
        ts = ts[valid_mask]
    if ts.empty:
        return 0.0, 0.0
    parsed_ratio = float(ts.notna().mean())
    parsed = ts.dropna()
    if parsed.empty:
        return parsed_ratio, 0.0
    plausible = parsed.between(pd.Timestamp("2000-01-01"), pd.Timestamp("2100-01-01"))
    plausible_ratio = float(plausible.mean())
    monotonic_bonus = 0.08 if parsed.is_monotonic_increasing or parsed.is_monotonic_decreasing else 0.0
    unique_bonus = min(0.07, float(parsed.nunique()) / max(len(parsed), 1))
    quality = parsed_ratio * 0.65 + plausible_ratio * 0.25 + monotonic_bonus + unique_bonus
    return parsed_ratio, min(1.0, quality)


def _time_text_score(values: pd.Series) -> float:
    text = values.astype(str).str.strip()
    if text.empty:
        return 0.0
    pattern = r"^\d{1,2}:\d{2}(:\d{2})?$"
    return float(text.str.match(pattern, na=False).mean())


def discover_files(root: Path, extensions: Iterable[str]) -> list[Path]:
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    ignored = {"outputs", "models", "gas_diagnosis", ".git", ".agents", ".codex"}
    files = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel_parts = set(p.relative_to(root).parts[:-1])
        if rel_parts & ignored:
            continue
        files.append(p)
    return sorted(files)


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return pd.read_excel(path, sheet_name=0)
    raise ValueError(f"Unsupported file type: {path.suffix}")


def _pressure_column_score(df: pd.DataFrame, col: object) -> tuple[float, int]:
    numeric = pd.to_numeric(df[col], errors="coerce")
    valid = numeric.dropna()
    valid_count = int(valid.size)
    if valid_count == 0:
        return 0.0, 0

    valid_ratio = valid_count / max(len(df), 1)
    name_score = 0.45 if _name_has_any(col, PRESSURE_NAME_KEYWORDS) else 0.0
    time_name_penalty = 0.75 if _name_has_any(col, TIME_NAME_KEYWORDS) else 0.0

    median = float(valid.median())
    q01 = float(valid.quantile(0.01))
    q99 = float(valid.quantile(0.99))
    plausible_score = 0.0
    if -0.5 <= q01 <= 20 and -0.5 <= median <= 20 and q99 <= 50:
        plausible_score = 0.35
    elif 0 <= q01 <= 50000 and 0 <= median <= 50000 and q99 <= 200000:
        plausible_score = 0.18

    timestamp_ratio, timestamp_quality = _datetime_quality(_to_datetime(df[col]))
    time_penalty = 0.55 if timestamp_ratio >= 0.8 and timestamp_quality >= 0.65 else 0.0

    score = valid_ratio + name_score + plausible_score - time_name_penalty - time_penalty
    return score, valid_count


def _pick_pressure_column(df: pd.DataFrame) -> str:
    candidates: list[tuple[float, int, str, int]] = []
    for idx, col in enumerate(df.columns):
        score, valid = _pressure_column_score(df, col)
        candidates.append((score, valid, str(col), idx))
    if not candidates:
        raise ValueError("no columns")
    candidates.sort(reverse=True)
    if candidates[0][0] <= 0:
        raise ValueError("cannot identify pressure column")
    return df.columns[candidates[0][3]]


def _pressure_columns(df: pd.DataFrame) -> list[str]:
    pressure_named: list[tuple[float, int, int, object]] = []
    for col in df.columns:
        score, valid = _pressure_column_score(df, col)
        if valid > 0 and _name_has_any(col, PRESSURE_NAME_KEYWORDS) and score > 0.35:
            pressure_named.append((score, valid, list(df.columns).index(col), col))
    if pressure_named:
        outlet_named = [item for item in pressure_named if _name_has_any(item[3], OUTLET_PRESSURE_NAME_KEYWORDS)]
        if outlet_named:
            outlet_named.sort(key=lambda item: (-item[0], item[2]))
            return [item[3] for item in outlet_named]
        non_inlet_named = [item for item in pressure_named if not _name_has_any(item[3], INLET_PRESSURE_NAME_KEYWORDS)]
        if non_inlet_named:
            non_inlet_named.sort(key=lambda item: (-item[0], item[2]))
            return [item[3] for item in non_inlet_named]
        pressure_named.sort(key=lambda item: (-item[0], item[2]))
        return [item[3] for item in pressure_named]
    return [_pick_pressure_column(df)]


def _pick_leakage_column(df: pd.DataFrame, pressure_col: str) -> str | None:
    candidates: list[tuple[float, int, object]] = []
    for idx, col in enumerate(df.columns):
        if col == pressure_col:
            continue
        if not _name_has_any(col, LEAKAGE_NAME_KEYWORDS):
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        valid = numeric.dropna()
        if valid.empty:
            continue
        timestamp_ratio, timestamp_quality = _datetime_quality(_to_datetime(df[col]))
        if timestamp_ratio >= 0.8 and timestamp_quality >= 0.65:
            continue
        valid_ratio = float(valid.size) / max(len(df), 1)
        nonnegative_ratio = float((valid >= 0).mean())
        score = valid_ratio + nonnegative_ratio + 0.35
        candidates.append((score, -idx, col))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _pick_closing_pressure_column(df: pd.DataFrame, pressure_col: str) -> str | None:
    candidates: list[tuple[float, int, object]] = []
    for idx, col in enumerate(df.columns):
        if col == pressure_col:
            continue
        if not _name_has_any(col, CLOSING_PRESSURE_NAME_KEYWORDS):
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        valid = numeric.dropna()
        if valid.empty:
            continue
        timestamp_ratio, timestamp_quality = _datetime_quality(_to_datetime(df[col]))
        if timestamp_ratio >= 0.8 and timestamp_quality >= 0.65:
            continue
        valid_ratio = float(valid.size) / max(len(df), 1)
        plausible_ratio = float(valid.between(0.0, 50.0).mean())
        score = valid_ratio + plausible_ratio + 0.45
        candidates.append((score, -idx, col))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _timestamp_candidates(df: pd.DataFrame, pressure_col: str) -> list[tuple[float, float, str, pd.Series]]:
    columns = list(df.columns)
    non_pressure = [c for c in columns if c != pressure_col]
    valid_pressure = pd.to_numeric(df[pressure_col], errors="coerce").notna()
    candidates: list[tuple[float, float, str, pd.Series]] = []

    def add_candidate(label: str, source: pd.Series, name_score: float = 0.0) -> None:
        parsed = _to_datetime(source)
        parsed_ratio, quality = _datetime_quality(parsed, valid_pressure)
        score = quality + name_score
        if parsed_ratio >= 0.2:
            candidates.append((score, parsed_ratio, label, parsed))

    for col in non_pressure:
        name_score = 0.18 if _name_has_any(col, TIME_NAME_KEYWORDS) else 0.0
        add_candidate(str(col), df[col], name_score=name_score)

    pair_indexes = set()
    for idx in range(len(non_pressure) - 1):
        pair_indexes.add((idx, idx + 1))

    pressure_idx = columns.index(pressure_col)
    if pressure_idx >= 2:
        pair_indexes.add((pressure_idx - 2, pressure_idx - 1))

    for left_idx, right_idx in sorted(pair_indexes):
        if left_idx < 0 or right_idx >= len(columns):
            continue
        left = columns[left_idx]
        right = columns[right_idx]
        if left == pressure_col or right == pressure_col:
            continue
        left_text = df[left].astype(str).str.strip()
        right_text = df[right].astype(str).str.strip()
        combined = left_text + " " + right_text
        name_score = 0.16 if _name_has_any(left, TIME_NAME_KEYWORDS) or _name_has_any(right, TIME_NAME_KEYWORDS) else 0.0
        if _time_text_score(df[left]) > 0.6 or _time_text_score(df[right]) > 0.6:
            name_score += 0.08
        add_candidate(f"{left} + {right}", combined, name_score=name_score)

    return candidates


def _best_timestamp_candidate(df: pd.DataFrame, pressure_col: str) -> tuple[pd.Series, str, float]:
    candidates = _timestamp_candidates(df, pressure_col)
    if not candidates:
        raise ValueError("cannot identify timestamp column")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, parsed_ratio, label, timestamp = candidates[0]
    if parsed_ratio < 0.2 or score < 0.25:
        raise ValueError("cannot parse timestamp")
    return timestamp, label, round(float(score), 4)


def _parse_timestamp(df: pd.DataFrame, pressure_col: str) -> pd.Series:
    timestamp, _, _ = _best_timestamp_candidate(df, pressure_col)
    return timestamp


def _parse_timestamp_near_pressure(df: pd.DataFrame, pressure_col: str) -> pd.Series:
    return _parse_timestamp(df, pressure_col)


def load_pressure_file(path: Path) -> pd.DataFrame:
    raw = _read_table(path)
    raw = raw.dropna(axis=1, how="all")
    if raw.empty:
        raise ValueError("empty table")

    frames = []
    for block_id, pressure_col in enumerate(_pressure_columns(raw), start=1):
        timestamp, timestamp_source, timestamp_score = _best_timestamp_candidate(raw, pressure_col)
        pressure = pd.to_numeric(raw[pressure_col], errors="coerce")
        leakage_col = _pick_leakage_column(raw, pressure_col)
        leakage = pd.to_numeric(raw[leakage_col], errors="coerce") if leakage_col is not None else None
        closing_col = _pick_closing_pressure_column(raw, pressure_col)
        closing_pressure = pd.to_numeric(raw[closing_col], errors="coerce") if closing_col is not None else None
        station = station_from_pressure_column(str(pressure_col).replace(".1", ""), path.stem)
        block = pd.DataFrame(
            {
                "station": station,
                "timestamp": timestamp,
                "pressure_kpa": pressure,
                "source_file": str(path),
                "source_block": block_id,
                "timestamp_source": timestamp_source,
                "seat_leakage": leakage if leakage is not None else pd.NA,
                "seat_leakage_column": str(leakage_col) if leakage_col is not None else "",
                "closing_pressure_kpa": closing_pressure if closing_pressure is not None else pd.NA,
                "closing_pressure_column": str(closing_col) if closing_col is not None else "",
            }
        )
        block = block.dropna(subset=["timestamp", "pressure_kpa"])
        if not block.empty:
            frames.append(block)

    if not frames:
        raise ValueError("no pressure rows parsed")
    data = pd.concat(frames, ignore_index=True)
    data = data.sort_values("timestamp")
    data = data.drop_duplicates(subset=["station", "timestamp", "pressure_kpa"], keep="last")
    return data.reset_index(drop=True)


def inspect_pressure_file(path: Path) -> dict:
    raw = _read_table(path)
    original_rows = int(len(raw))
    raw = raw.dropna(axis=1, how="all")
    if raw.empty:
        raise ValueError("empty table")

    blocks = []
    total_valid = 0
    total_invalid = 0
    for block_id, pressure_col in enumerate(_pressure_columns(raw), start=1):
        timestamp, timestamp_source, timestamp_score = _best_timestamp_candidate(raw, pressure_col)
        pressure = pd.to_numeric(raw[pressure_col], errors="coerce")
        leakage_col = _pick_leakage_column(raw, pressure_col)
        closing_col = _pick_closing_pressure_column(raw, pressure_col)
        station = station_from_pressure_column(str(pressure_col).replace(".1", ""), path.stem)
        valid_mask = timestamp.notna() & pressure.notna()
        valid_count = int(valid_mask.sum())
        invalid_count = int(len(raw) - valid_count)
        total_valid += valid_count
        total_invalid += invalid_count

        if valid_count:
            valid_time = timestamp[valid_mask]
            valid_pressure = pressure[valid_mask]
            start = str(valid_time.min())
            end = str(valid_time.max())
            min_pressure = round(float(valid_pressure.min()), 4)
            max_pressure = round(float(valid_pressure.max()), 4)
        else:
            start = ""
            end = ""
            min_pressure = None
            max_pressure = None

        blocks.append(
            {
                "block": block_id,
                "station": station,
                "timestamp_source": timestamp_source,
                "timestamp_score": timestamp_score,
                "pressure_column": str(pressure_col),
                "seat_leakage_column": str(leakage_col) if leakage_col is not None else "",
                "closing_pressure_column": str(closing_col) if closing_col is not None else "",
                "valid_rows": valid_count,
                "invalid_rows": invalid_count,
                "start": start,
                "end": end,
                "min_pressure": min_pressure,
                "max_pressure": max_pressure,
            }
        )

    warnings_list = []
    if total_valid == 0:
        warnings_list.append("未识别到有效时间和压力数据")
    if total_valid < 100:
        warnings_list.append("有效样本较少，诊断稳定性会下降")
    if total_invalid > 0 and total_valid > 0 and total_invalid / (total_valid + total_invalid) > 0.2:
        warnings_list.append("无效行占比较高，建议检查时间列和压力列")
    if len(blocks) > 1:
        warnings_list.append("检测到多个压力数据块，系统会合并后诊断")

    return {
        "file_name": path.name,
        "file_type": path.suffix.lower().lstrip("."),
        "raw_rows": original_rows,
        "raw_columns": int(len(raw.columns)),
        "columns": [str(col) for col in raw.columns],
        "blocks": blocks,
        "valid_rows": total_valid,
        "invalid_rows": total_invalid,
        "warnings": warnings_list,
    }


def load_pressure_files(paths: Iterable[Path]) -> LoadResult:
    frames = []
    failed: list[str] = []
    loaded = 0
    for path in paths:
        try:
            frame = load_pressure_file(path)
            if not frame.empty:
                frames.append(frame)
                loaded += 1
        except Exception as exc:  # noqa: BLE001 - keep batch import resilient.
            failed.append(f"{path}: {exc}")
    if frames:
        data = pd.concat(frames, ignore_index=True)
        data = data.sort_values(["station", "timestamp"]).reset_index(drop=True)
    else:
        data = pd.DataFrame(columns=["station", "timestamp", "pressure_kpa", "source_file"])
    return LoadResult(data=data, files_loaded=loaded, files_failed=failed)
