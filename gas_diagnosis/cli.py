"""Command-line interface for the gas regulator diagnosis MVP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .ai import anomaly_score, build_baseline
from .data_loader import discover_files, load_pressure_file, load_pressure_files
from .features import daily_feature_table, extract_features
from .performance import evaluate_performance
from .report import write_reports
from .rules import evaluate_rules, fuse_health_level


def _parse_extensions(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _performance_params_from_args(args: argparse.Namespace) -> dict:
    mapping = {
        "set_pressure_kpa": getattr(args, "set_pressure_kpa", None),
        "ac_percent": getattr(args, "ac_percent", None),
        "sg_percent": getattr(args, "sg_percent", None),
        "extreme_sample_n": getattr(args, "extreme_sample_n", None),
        "seat_leak_limit": getattr(args, "seat_leak_limit", None),
        "seat_leak_surrogate_limit_kpa": getattr(args, "seat_leak_surrogate_limit_kpa", None),
        "ac_kpa": getattr(args, "ac_kpa", None),
        "sg_kpa": getattr(args, "sg_kpa", None),
        "seat_leak_limit_kpa": getattr(args, "seat_leak_limit_kpa", None),
    }
    return {key: value for key, value in mapping.items() if value is not None}


def build_baseline_command(args: argparse.Namespace) -> None:
    root = Path(args.history_root)
    files = discover_files(root, _parse_extensions(args.extensions))
    loaded = load_pressure_files(files)
    daily = daily_feature_table(loaded.data)
    baseline = build_baseline(daily)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "baseline": str(output),
        "files_found": len(files),
        "files_loaded": loaded.files_loaded,
        "files_failed": len(loaded.files_failed),
        "daily_windows": int(len(daily)),
        "stations": len(baseline.get("stations", {})),
    }, ensure_ascii=False, indent=2))


def _is_healthy_window(row: pd.Series, min_count: int) -> tuple[bool, list[str]]:
    reasons = []
    if int(row.get("count", 0) or 0) < min_count:
        reasons.append("采样点过少")
    if float(row.get("min", 0.0) or 0.0) <= 0.05:
        reasons.append("存在0值或接近0压力")
    if float(row.get("max", 0.0) or 0.0) >= 3.0:
        reasons.append("最大压力超过3.0KPa")
    if float(row.get("low_200_ratio", 0.0) or 0.0) > 0:
        reasons.append("存在低于2.0KPa")
    if float(row.get("high_275_ratio", 0.0) or 0.0) > 0.05:
        reasons.append("超过2.75KPa比例偏高")
    if float(row.get("std", 0.0) or 0.0) > 0.35:
        reasons.append("波动标准差过大")
    if reasons:
        return False, reasons
    findings = evaluate_rules(row.to_dict())
    severe = [item for item in findings if item["severity"] >= 4]
    if severe:
        return False, [f"触发风险规则:{'/'.join(item['code'] for item in severe)}"]
    return True, []


def build_healthy_baseline_command(args: argparse.Namespace) -> None:
    root = Path(args.history_root)
    files = discover_files(root, _parse_extensions(args.extensions))
    loaded = load_pressure_files(files)
    daily = daily_feature_table(loaded.data)

    keep_rows = []
    audit_rows = []
    for _, row in daily.iterrows():
        keep, reasons = _is_healthy_window(row, min_count=args.min_count)
        audit = {
            "station": row.get("station"),
            "start": row.get("start"),
            "end": row.get("end"),
            "count": row.get("count"),
            "mean": row.get("mean"),
            "min": row.get("min"),
            "max": row.get("max"),
            "std": row.get("std"),
            "kept": keep,
            "reasons": ";".join(reasons),
        }
        audit_rows.append(audit)
        if keep:
            keep_rows.append(row)

    healthy = pd.DataFrame(keep_rows)
    baseline = build_baseline(healthy)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    audit_path = Path(args.audit_output)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audit_rows).to_csv(audit_path, index=False, encoding="utf-8-sig")

    print(json.dumps({
        "baseline": str(output),
        "audit": str(audit_path),
        "files_found": len(files),
        "files_loaded": loaded.files_loaded,
        "files_failed": len(loaded.files_failed),
        "daily_windows_total": int(len(daily)),
        "daily_windows_kept": int(len(healthy)),
        "daily_windows_removed": int(len(daily) - len(healthy)),
        "stations": len(baseline.get("stations", {})),
    }, ensure_ascii=False, indent=2))


def _load_or_build_baseline(args: argparse.Namespace) -> dict:
    if args.baseline and Path(args.baseline).exists():
        return json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    if not args.history_root:
        return {"feature_keys": [], "stations": {}, "global": {}}
    files = discover_files(Path(args.history_root), _parse_extensions(args.extensions))
    loaded = load_pressure_files(files)
    daily = daily_feature_table(loaded.data)
    return build_baseline(daily)


def diagnose_frame(data: pd.DataFrame, baseline: dict, performance_params: dict | None = None) -> dict:
    features = extract_features(data)
    findings = evaluate_rules(features)
    ai = anomaly_score(features, baseline)
    performance = evaluate_performance(data, features, performance_params)
    model_score = float((ai.get("isolation_forest") or {}).get("score") or 0.0)
    trend_score = min(1.0, abs(float(features.get("slope_per_day") or 0.0)) / 0.04)
    health = fuse_health_level(
        findings,
        ai_score=float(ai["score"]),
        trend_score=trend_score,
        model_score=model_score,
        ai_details=ai,
        performance=performance,
    )
    return {
        "features": features,
        "performance": performance,
        "findings": findings,
        "ai": ai,
        "health": health,
        "notes": [
            "诊断基于现有压力数据、技术要求、健康基线和组合校准算法生成。",
            "若设备设定参数缺失，结论主要依赖历史基线和数据驱动诊断。",
        ],
    }


def diagnose_command(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    baseline = _load_or_build_baseline(args)
    data = load_pressure_file(input_path)
    result = diagnose_frame(data, baseline, _performance_params_from_args(args))
    outputs = write_reports(result, data, Path(args.output_dir))
    print(json.dumps({
        "station": result["features"]["station"],
        "health": result["health"],
        "findings": [f"{item['code']}:{item['name']}" for item in result["findings"]],
        "outputs": outputs,
    }, ensure_ascii=False, indent=2))


def batch_command(args: argparse.Namespace) -> None:
    baseline = _load_or_build_baseline(args)
    performance_params = _performance_params_from_args(args)
    files = discover_files(Path(args.input_root), _parse_extensions(args.extensions))
    rows = []
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        try:
            data = load_pressure_file(path)
            result = diagnose_frame(data, baseline, performance_params)
            write_reports(result, data, output_dir / "reports")
            perf = result.get("performance") or {}
            perf_items = {item.get("code"): item for item in perf.get("items", [])}
            rows.append({
                "file": str(path),
                "station": result["features"]["station"],
                "health_level": result["health"]["level"],
                "health_label": result["health"]["label"],
                "performance_level": (perf.get("overall") or {}).get("level"),
                "performance_label": (perf.get("overall") or {}).get("label"),
                "steady_level": (perf_items.get("P01") or {}).get("level"),
                "steady_ratio": (perf_items.get("P01") or {}).get("ratio"),
                "closing_level": (perf_items.get("P02") or {}).get("level"),
                "closing_ratio": (perf_items.get("P02") or {}).get("ratio"),
                "seat_seal_level": (perf_items.get("P03") or {}).get("level"),
                "seat_seal_ratio": (perf_items.get("P03") or {}).get("ratio"),
                "diagnosis_mode": result["health"].get("diagnosis_mode"),
                "risk_score": result["health"]["risk_score"],
                "ai_score": result["health"]["ai_score"],
                "baseline_score": result.get("ai", {}).get("baseline_score"),
                "model_anomaly_score": (result.get("ai", {}).get("isolation_forest") or {}).get("score"),
                "model_band": (result.get("ai", {}).get("isolation_forest") or {}).get("band_label"),
                "model_percentile": (result.get("ai", {}).get("isolation_forest") or {}).get("percentile"),
                "knn_anomaly_score": (result.get("ai", {}).get("knn") or {}).get("score"),
                "knn_band": (result.get("ai", {}).get("knn") or {}).get("band_label"),
                "knn_percentile": (result.get("ai", {}).get("knn") or {}).get("percentile"),
                "findings": ";".join(f"{f['code']}:{f['name']}" for f in result["findings"]),
                "mean": result["features"]["mean"],
                "min": result["features"]["min"],
                "max": result["features"]["max"],
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"file": str(path), "error": str(exc)})
    summary = pd.DataFrame(rows)
    out_csv = output_dir / "batch_diagnosis_summary.csv"
    summary.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(json.dumps({"files": len(files), "summary": str(out_csv)}, ensure_ascii=False, indent=2))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="燃气调压器健康诊断 MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    p_baseline = sub.add_parser("build-baseline", help="从历史数据建立AI基线")
    p_baseline.add_argument("--history-root", required=True)
    p_baseline.add_argument("--extensions", default=".csv", help="逗号分隔，例如 .csv,.xlsx")
    p_baseline.add_argument("--output", default="models/baseline.json")
    p_baseline.set_defaults(func=build_baseline_command)

    p_healthy = sub.add_parser("build-healthy-baseline", help="从历史数据筛选健康窗口建立AI健康基线")
    p_healthy.add_argument("--history-root", required=True)
    p_healthy.add_argument("--extensions", default=".csv,.xlsx")
    p_healthy.add_argument("--output", default="models/baseline_healthy.json")
    p_healthy.add_argument("--audit-output", default="outputs/baseline_healthy_audit.csv")
    p_healthy.add_argument("--min-count", type=int, default=60, help="日窗口最少采样点")
    p_healthy.set_defaults(func=build_healthy_baseline_command)

    p_diag = sub.add_parser("diagnose", help="诊断一份新输入数据")
    p_diag.add_argument("--input", required=True)
    p_diag.add_argument("--history-root", default=None, help="未提供baseline时，用该目录现场建立AI基线")
    p_diag.add_argument("--baseline", default=None)
    p_diag.add_argument("--extensions", default=".csv")
    p_diag.add_argument("--output-dir", default="outputs/diagnosis")
    p_diag.add_argument("--set-pressure-kpa", type=float, default=None, help="出口压力设定值，单位KPa")
    p_diag.add_argument("--ac-percent", type=float, default=None, help="稳压精度等级AC，百分比")
    p_diag.add_argument("--sg-percent", type=float, default=None, help="关闭压力等级SG，百分比")
    p_diag.add_argument("--extreme-sample-n", type=int, default=None, help="AC测算时日间最高/最低压力点均值所取点数N")
    p_diag.add_argument("--seat-leak-limit", type=float, default=None, help="阀座泄漏量限值，单位与导入泄漏量字段一致")
    p_diag.add_argument("--seat-leak-surrogate-limit-kpa", type=float, default=None, help="无泄漏量字段时的压力采样估算限值，单位KPa")
    p_diag.add_argument("--ac-kpa", type=float, default=None, help="兼容旧版：稳压性能允许偏差AC，单位KPa")
    p_diag.add_argument("--sg-kpa", type=float, default=None, help="兼容旧版：关闭压力性能允许偏差SG，单位KPa")
    p_diag.add_argument("--seat-leak-limit-kpa", type=float, default=None, help="兼容旧版：压力采样估算限值，单位KPa")
    p_diag.set_defaults(func=diagnose_command)

    p_batch = sub.add_parser("batch", help="批量诊断一个目录")
    p_batch.add_argument("--input-root", required=True)
    p_batch.add_argument("--history-root", default=None)
    p_batch.add_argument("--baseline", default=None)
    p_batch.add_argument("--extensions", default=".csv")
    p_batch.add_argument("--output-dir", default="outputs/diagnosis_batch")
    p_batch.add_argument("--set-pressure-kpa", type=float, default=None, help="出口压力设定值，单位KPa")
    p_batch.add_argument("--ac-percent", type=float, default=None, help="稳压精度等级AC，百分比")
    p_batch.add_argument("--sg-percent", type=float, default=None, help="关闭压力等级SG，百分比")
    p_batch.add_argument("--extreme-sample-n", type=int, default=None, help="AC测算时日间最高/最低压力点均值所取点数N")
    p_batch.add_argument("--seat-leak-limit", type=float, default=None, help="阀座泄漏量限值，单位与导入泄漏量字段一致")
    p_batch.add_argument("--seat-leak-surrogate-limit-kpa", type=float, default=None, help="无泄漏量字段时的压力采样估算限值，单位KPa")
    p_batch.add_argument("--ac-kpa", type=float, default=None, help="兼容旧版：稳压性能允许偏差AC，单位KPa")
    p_batch.add_argument("--sg-kpa", type=float, default=None, help="兼容旧版：关闭压力性能允许偏差SG，单位KPa")
    p_batch.add_argument("--seat-leak-limit-kpa", type=float, default=None, help="兼容旧版：压力采样估算限值，单位KPa")
    p_batch.set_defaults(func=batch_command)

    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
