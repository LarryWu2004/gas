"""Rule engine aligned with the technical diagnosis requirements."""

from __future__ import annotations

from .config import DEFAULT_THRESHOLDS, HEALTH_LEVELS


def _add(findings: list[dict], code: str, name: str, severity: int, maintenance: str, evidence: str, cause: str) -> None:
    findings.append(
        {
            "code": code,
            "name": name,
            "severity": severity,
            "maintenance": maintenance,
            "evidence": evidence,
            "suspected_cause": cause,
        }
    )


def evaluate_rules(features: dict, thresholds: dict | None = None) -> list[dict]:
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    findings: list[dict] = []

    max_p = features.get("max") or 0.0
    min_p = features.get("min") or 0.0
    mean_p = features.get("mean") or 0.0
    high_275 = features.get("high_275_ratio") or 0.0
    high_300 = features.get("high_300_ratio") or 0.0
    high_310 = features.get("high_310_ratio") or 0.0
    low_200 = features.get("low_200_ratio") or 0.0
    night_max = features.get("night_max") or 0.0
    night_high_300 = features.get("night_high_300_ratio") or 0.0
    peak_low = features.get("peak_low_200_ratio") or 0.0
    wave_interval = features.get("wave_interval_min")
    wave_count = features.get("wave_count") or 0
    slope = features.get("slope_per_day") or 0.0

    if min_p <= t["sensor_zero_kpa"] or max_p >= t["sensor_spike_kpa"]:
        _add(
            findings,
            "R08",
            "采集异常或极端压力值",
            4,
            "先复核传感器、采集链路和原始数据",
            f"最小值={min_p:.3f}KPa，最大值={max_p:.3f}KPa",
            "传感器异常、采集缺失、瞬时尖峰或真实极端工况",
        )

    longest_high_300 = features.get("longest_high_300_min") or 0.0

    if max_p >= t["high_near_release_kpa"] and (high_275 >= 0.01 or high_300 >= 0.005 or longest_high_300 >= 3.0):
        severity = 5 if high_310 >= 0.01 or max_p >= 3.5 else 4
        _add(
            findings,
            "R01",
            "压力快速偏高 / 放散风险",
            severity,
            "立即处理" if severity >= 5 else "立即维保",
            f"最大值={max_p:.3f}KPa，超过2.75KPa比例={high_275:.1%}，超过3.0KPa比例={high_300:.1%}，连续超过3.0KPa最长约{longest_high_300:.1f}分钟",
            "主体不严、阀口受损、阀口垫老化变形",
        )

    if mean_p >= 2.75 or high_300 >= 0.05:
        _add(
            findings,
            "R02",
            "运行压力偏高",
            4,
            "立即",
            f"平均值={mean_p:.3f}KPa，超过3.0KPa比例={high_300:.1%}",
            "阀口、阀口垫、橡胶件损伤，或初始压力设定偏高",
        )

    if low_200 >= 0.05 or peak_low >= 0.10:
        _add(
            findings,
            "R03",
            "高峰期压力偏低",
            4,
            "1个月以内维保",
            f"低于2.0KPa比例={low_200:.1%}，高峰期低压比例={peak_low:.1%}",
            "阀杆卡顿、主体弹簧变弱、阀口无法正常开启",
        )

    if night_max >= t["high_near_release_kpa"] or night_high_300 >= 0.05:
        _add(
            findings,
            "R04",
            "低峰期关闭不严 / 持续升压",
            5 if night_max >= t["high_release_kpa"] else 4,
            "立即维保",
            f"夜间最大值={night_max:.3f}KPa，夜间超过3.0KPa比例={night_high_300:.1%}",
            "主体阀口关闭不严、橡胶件或皮膜老化",
        )

    if night_max >= t["high_release_kpa"] and high_310 >= 0.01:
        _add(
            findings,
            "R05",
            "关闭压力偏高",
            4,
            "1个月内维保",
            f"夜间最大值={night_max:.3f}KPa，超过3.1KPa比例={high_310:.1%}",
            "阀口垫老化，关闭压力偏高",
        )

    if wave_interval is not None and wave_count >= 6 and wave_interval <= t["wave_abnormal_interval_min"]:
        _add(
            findings,
            "R06",
            "波动频率异常",
            3,
            "6个月内维保",
            f"波动次数={wave_count}，典型波动间隔={wave_interval:.1f}分钟",
            "驱动器轻微卡顿或皮膜老化",
        )

    if slope <= -0.01:
        _add(
            findings,
            "R07",
            "长期缓慢下降趋势",
            3,
            "按趋势推算，建议12个月内关注或维保",
            f"压力趋势斜率={slope:.4f}KPa/日",
            "弹簧老化偏软或设定状态逐步漂移",
        )

    return findings


def fuse_health_level(
    findings: list[dict],
    ai_score: float,
    trend_score: float = 0.0,
    model_score: float = 0.0,
    ai_details: dict | None = None,
    performance: dict | None = None,
) -> dict:
    ai_details = ai_details or {}
    isolation = ai_details.get("isolation_forest") or {}
    knn = ai_details.get("knn") or {}
    baseline_score = float(ai_details.get("baseline_score", ai_score) or 0.0)
    top_features = ai_details.get("top_features") or []
    max_feature_z = max([float(item.get("z", 0.0) or 0.0) for item in top_features], default=0.0)
    top_feature = top_features[0] if top_features else {}

    max_rule = max([item["severity"] for item in findings], default=1)
    rule_score = max(0.0, (max_rule - 1) / 4.0)
    knn_score = float(knn.get("score", 0.0) or 0.0)
    performance_overall = (performance or {}).get("overall") or {}
    performance_items = (performance or {}).get("items") or []
    performance_level = int(performance_overall.get("level") or 0)
    item_scores = []
    for item in performance_items:
        score = max(0.0, min(1.0, float(item.get("risk_score", 0.0) or 0.0) / 100.0))
        status = str(item.get("confirmation_status") or "")
        if status in {"pressure_surrogate", "low_flow_surrogate"}:
            score *= 0.68
        item_scores.append(score)
    performance_max_score = max(item_scores, default=0.0)
    performance_avg_score = sum(item_scores) / len(item_scores) if item_scores else 0.0
    performance_level_floor = {1: 0.05, 2: 0.18, 3: 0.35, 4: 0.60, 5: 0.80}.get(performance_level, 0.0)
    performance_score = max(
        performance_level_floor,
        min(1.0, 0.70 * performance_max_score + 0.30 * performance_avg_score),
    )

    anomaly_score = max(float(ai_score), float(model_score or 0.0), knn_score)
    auxiliary_score = min(
        1.0,
        max(
            0.46 * rule_score
            + 0.18 * baseline_score
            + 0.16 * float(model_score or 0.0)
            + 0.16 * knn_score
            + 0.04 * float(trend_score),
            0.52 * anomaly_score,
        ),
    )
    risk_score = min(1.0, 0.85 * performance_score + 0.15 * auxiliary_score)

    isolation_band = str(isolation.get("band") or "")
    isolation_band_label = str(isolation.get("band_label") or "")
    isolation_percentile = isolation.get("percentile")
    isolation_score = float(model_score or 0.0)
    isolation_level = _score_level(isolation_band, isolation_score)

    knn_band = str(knn.get("band") or "")
    knn_band_label = str(knn.get("band_label") or "")
    knn_percentile = knn.get("percentile")
    knn_level = _score_level(knn_band, knn_score)
    model_level = max(isolation_level, knn_level)

    baseline_value = max(float(ai_score), baseline_score)
    if baseline_value >= 0.75 or max_feature_z >= 5.0:
        baseline_level = 4
    elif baseline_value >= 0.55 or max_feature_z >= 3.5:
        baseline_level = 3
    elif baseline_value >= 0.30 or max_feature_z >= 2.0:
        baseline_level = 2
    else:
        baseline_level = 1

    if trend_score >= 0.50:
        trend_level = 3
    elif trend_score >= 0.25:
        trend_level = 2
    else:
        trend_level = 1

    reasons: list[str] = []
    if findings:
        severe = [item for item in findings if int(item.get("severity", 0)) == max_rule]
        codes = "、".join(f"{item.get('code')} {item.get('name')}" for item in severe[:3])
        reasons.append(f"规则层：触发最高{max_rule}级风险规则（{codes}），作为安全底线。")
    else:
        reasons.append("规则层：未触发超压、低压、持续升压等明确安全规则。")

    if isolation.get("method") and isolation.get("method") != "no_isolation_forest":
        percent_text = f"，约处于健康基线第{isolation_percentile}分位" if isolation_percentile is not None else ""
        reasons.append(
            f"IF层：Isolation Forest 判定为{isolation_band_label or '未分区'}"
            f"{percent_text}，IF异常分 {isolation_score:.4f}。"
        )
    else:
        reasons.append("IF层：当前未使用可用的 Isolation Forest 异常分。")

    if knn.get("method") and knn.get("method") != "no_knn_samples":
        percent_text = f"，约处于健康基线第{knn_percentile}分位" if knn_percentile is not None else ""
        reasons.append(
            f"KNN层：当前样本与最近{int(knn.get('nearest_count') or 0)}个健康样本的距离判定为"
            f"{knn_band_label or '未分区'}{percent_text}，KNN异常分 {knn_score:.4f}。"
        )
    else:
        reasons.append("KNN层：当前健康样本不足，未形成有效近邻距离证据。")

    if top_feature:
        reasons.append(
            "基线层：当前特征与健康基线的综合偏离分 "
            f"{baseline_score:.4f}，最大偏离特征为 {top_feature.get('feature')} "
            f"(Z={float(top_feature.get('z', 0.0) or 0.0):.3f})。"
        )
    else:
        reasons.append(f"基线层：当前健康基线偏离分 {baseline_score:.4f}。")

    if trend_level >= 2:
        reasons.append(f"趋势层：压力趋势分 {float(trend_score):.4f}，存在需要关注的变化。")

    ai_evidence_levels = [isolation_level, knn_level, baseline_level]
    strong_evidence = sum(1 for level_value in ai_evidence_levels if level_value >= 4)
    obvious_evidence = sum(1 for level_value in ai_evidence_levels if level_value >= 3)
    mild_evidence = sum(1 for level_value in ai_evidence_levels if level_value >= 2)
    model_consensus = isolation_level >= 3 and knn_level >= 3

    if max_rule >= 5:
        level = 5
    elif max_rule >= 4:
        level = 5 if strong_evidence >= 2 else 4
    elif max_rule == 3:
        level = 4 if strong_evidence >= 2 or (model_consensus and baseline_level >= 3) else 3
    elif strong_evidence >= 2 and model_consensus:
        level = 4
    elif obvious_evidence >= 2 or (model_consensus and baseline_level >= 2):
        level = 3
    elif mild_evidence >= 2 or max(model_level, baseline_level, trend_level) >= 2:
        level = 2
    else:
        level = 1

    if level >= 4 and max_rule < 4:
        reasons.append("组合结论：Isolation Forest、KNN 与基线偏离形成强一致证据，虽未触发硬规则，仍提升为风险级。")
    elif level == 3 and max_rule < 3:
        reasons.append("组合结论：至少两类证据提示异常，但未达到明确安全风险，判为亚健康。")
    elif level <= 2 and model_level >= 3:
        reasons.append("组合结论：单类辅助证据提示异常但缺少一致性证据，暂不直接升为风险级。")

    if performance_level and performance_level <= 2 and level == 3 and max_rule <= 3:
        if strong_evidence <= 1 or not model_consensus:
            level = 2
            reasons.append("组合结论：核心性能指标为优良/合格，辅助层异常信号尚不构成一致证据，最终降为健康级。")
        else:
            reasons.append("组合结论：核心性能指标优良/合格，但辅助层存在多类强异常且一致，维持亚健康以提示关注。")

    risk_floor = {1: 0.0, 2: 0.18, 3: 0.35, 4: 0.60, 5: 0.80}[level]
    risk_score = max(float(risk_score), risk_floor)

    auxiliary_level = level
    if performance_level:
        level = max(level, performance_level)
        risk_floor = {1: 0.0, 2: 0.18, 3: 0.35, 4: 0.60, 5: 0.80}[level]
        performance_floor = {1: 0.05, 2: 0.18, 3: 0.35, 4: 0.60, 5: 0.80}[performance_level]
        risk_score = max(float(risk_score), risk_floor, performance_floor)
        reasons.insert(
            0,
            f"核心性能层：按技术要求计算稳压性能、关闭压力性能、阀座密封性能，综合结果为{performance_level}级（{performance_overall.get('label')}），{performance_overall.get('basis')}。",
        )

    return {
        "level": level,
        "label": HEALTH_LEVELS[level],
        "risk_score": round(float(risk_score), 4),
        "rule_score": round(float(rule_score), 4),
        "performance_score": round(float(performance_score), 4),
        "auxiliary_score": round(float(auxiliary_score), 4),
        "ai_score": round(float(ai_score), 4),
        "baseline_score": round(float(baseline_score), 4),
        "model_score": round(float(model_score or 0.0), 4),
        "knn_score": round(float(knn_score), 4),
        "trend_score": round(float(trend_score), 4),
        "diagnosis_mode": "组合校准诊断V3",
        "performance_level": performance_level or None,
        "auxiliary_level": int(auxiliary_level),
        "evidence": {
            "rule_level": int(max_rule),
            "model_level": int(model_level),
            "isolation_level": int(isolation_level),
            "knn_level": int(knn_level),
            "baseline_level": int(baseline_level),
            "trend_level": int(trend_level),
            "isolation_band": isolation_band,
            "isolation_band_label": isolation_band_label,
            "isolation_percentile": isolation_percentile,
            "knn_band": knn_band,
            "knn_band_label": knn_band_label,
            "knn_percentile": knn_percentile,
            "strong_evidence_count": int(strong_evidence),
            "obvious_evidence_count": int(obvious_evidence),
            "model_consensus": bool(model_consensus),
            "max_feature_z": round(float(max_feature_z), 3),
        },
        "decision_reasons": reasons,
    }


def _score_level(band: str, score: float) -> int:
    if band == "strong" or score >= 0.85:
        return 4
    if band == "obvious" or score >= 0.60:
        return 3
    if band == "mild" or score >= 0.30:
        return 2
    return 1
