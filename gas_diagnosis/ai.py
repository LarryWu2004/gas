"""Small, dependency-light AI helpers for unsupervised diagnosis."""

from __future__ import annotations

import math
import base64
import pickle
import random
from typing import Iterable

import numpy as np
import pandas as pd

from .config import FEATURE_KEYS


try:
    from sklearn.ensemble import IsolationForest as SklearnIsolationForest
    from sklearn.preprocessing import RobustScaler
    import sklearn
except Exception:  # noqa: BLE001 - keep the project runnable without optional sklearn.
    SklearnIsolationForest = None
    RobustScaler = None
    sklearn = None


def _feature_vector(row: dict | pd.Series, keys: Iterable[str] = FEATURE_KEYS) -> np.ndarray:
    values = []
    for key in keys:
        value = row.get(key, 0.0) if isinstance(row, dict) else row.get(key, 0.0)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            value = 0.0
        values.append(float(value))
    return np.array(values, dtype=float)


def _robust_center_scale(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    center = np.nanmedian(matrix, axis=0)
    mad = np.nanmedian(np.abs(matrix - center), axis=0)
    scale = np.where(mad < 1e-9, np.nanstd(matrix, axis=0), mad * 1.4826)
    scale = np.where(scale < 1e-9, 1.0, scale)
    return center, scale


def _average_path_length(sample_count: int) -> float:
    if sample_count <= 1:
        return 0.0
    if sample_count == 2:
        return 1.0
    return 2.0 * (math.log(sample_count - 1) + 0.5772156649) - 2.0 * (sample_count - 1) / sample_count


def _build_isolation_tree(matrix: np.ndarray, rng: random.Random, depth: int, max_depth: int, nodes: list[dict]) -> int:
    node_index = len(nodes)
    size = int(len(matrix))
    nodes.append({"s": size})
    if size <= 1 or depth >= max_depth:
        return node_index

    spreads = np.nanmax(matrix, axis=0) - np.nanmin(matrix, axis=0)
    feature_candidates = [idx for idx, spread in enumerate(spreads) if spread > 1e-9]
    if not feature_candidates:
        return node_index

    feature = rng.choice(feature_candidates)
    min_value = float(np.nanmin(matrix[:, feature]))
    max_value = float(np.nanmax(matrix[:, feature]))
    if not min_value < max_value:
        return node_index

    threshold = rng.uniform(min_value, max_value)
    left_mask = matrix[:, feature] < threshold
    if left_mask.all() or (~left_mask).all():
        return node_index

    left_index = _build_isolation_tree(matrix[left_mask], rng, depth + 1, max_depth, nodes)
    right_index = _build_isolation_tree(matrix[~left_mask], rng, depth + 1, max_depth, nodes)
    nodes[node_index] = {
        "f": int(feature),
        "t": round(float(threshold), 8),
        "l": int(left_index),
        "r": int(right_index),
        "s": size,
    }
    return node_index


def _tree_path_length(vector: np.ndarray, nodes: list[dict], node_index: int = 0, depth: int = 0) -> float:
    node = nodes[node_index]
    if "f" not in node:
        return depth + _average_path_length(int(node.get("s", 1)))
    next_index = node["l"] if vector[int(node["f"])] < float(node["t"]) else node["r"]
    return _tree_path_length(vector, nodes, int(next_index), depth + 1)


def _isolation_raw_score(vector: np.ndarray, forest: dict) -> float:
    trees = forest.get("trees", [])
    if not trees:
        return 0.0
    paths = [_tree_path_length(vector, tree.get("nodes", [])) for tree in trees if tree.get("nodes")]
    if not paths:
        return 0.0
    c_n = _average_path_length(int(forest.get("max_samples", 1)))
    if c_n <= 1e-9:
        return 0.0
    return float(2.0 ** (-float(np.mean(paths)) / c_n))


def build_isolation_forest(
    matrix: np.ndarray,
    *,
    feature_keys: Iterable[str] = FEATURE_KEYS,
    n_trees: int = 80,
    max_samples: int = 256,
    random_state: int = 42,
) -> dict:
    if matrix.size == 0:
        return {}

    matrix = np.nan_to_num(matrix.astype(float), nan=0.0, posinf=0.0, neginf=0.0)
    center, scale = _robust_center_scale(matrix)
    normalized = (matrix - center) / scale
    sample_count = int(len(normalized))
    actual_max_samples = max(2, min(int(max_samples), sample_count))
    max_depth = int(math.ceil(math.log2(actual_max_samples)))
    rng = random.Random(random_state)

    trees = []
    for _ in range(int(n_trees)):
        sample_indexes = [rng.randrange(sample_count) for _ in range(actual_max_samples)]
        sample = normalized[sample_indexes]
        nodes: list[dict] = []
        _build_isolation_tree(sample, rng, 0, max_depth, nodes)
        trees.append({"nodes": nodes})

    forest = {
        "method": "lightweight_isolation_forest",
        "feature_keys": list(feature_keys),
        "center": center.round(6).tolist(),
        "scale": scale.round(6).tolist(),
        "n_trees": int(n_trees),
        "max_samples": int(actual_max_samples),
        "max_depth": int(max_depth),
        "random_state": int(random_state),
        "trees": trees,
    }

    raw_scores = np.array([_isolation_raw_score(row, forest) for row in normalized], dtype=float)
    forest["calibration"] = {
        "p50": round(float(np.nanpercentile(raw_scores, 50)), 6),
        "p90": round(float(np.nanpercentile(raw_scores, 90)), 6),
        "p95": round(float(np.nanpercentile(raw_scores, 95)), 6),
        "p99": round(float(np.nanpercentile(raw_scores, 99)), 6),
    }
    return forest


def _pickle_to_base64(value: object) -> str:
    return base64.b64encode(pickle.dumps(value)).decode("ascii")


def _pickle_from_base64(value: str) -> object:
    return pickle.loads(base64.b64decode(value.encode("ascii")))


def _build_sklearn_isolation_forest(
    matrix: np.ndarray,
    *,
    feature_keys: Iterable[str] = FEATURE_KEYS,
    n_estimators: int = 160,
    max_samples: int = 256,
    random_state: int = 42,
) -> dict:
    if SklearnIsolationForest is None or RobustScaler is None or sklearn is None:
        return {}
    if matrix.size == 0:
        return {}

    matrix = np.nan_to_num(matrix.astype(float), nan=0.0, posinf=0.0, neginf=0.0)
    actual_max_samples = max(2, min(int(max_samples), int(len(matrix))))
    scaler = RobustScaler()
    normalized = scaler.fit_transform(matrix)
    model = SklearnIsolationForest(
        n_estimators=int(n_estimators),
        max_samples=actual_max_samples,
        contamination="auto",
        random_state=int(random_state),
        n_jobs=1,
    )
    model.fit(normalized)
    raw_scores = -model.score_samples(normalized)

    return {
        "method": "sklearn_isolation_forest",
        "sklearn_version": sklearn.__version__,
        "feature_keys": list(feature_keys),
        "n_estimators": int(n_estimators),
        "max_samples": int(actual_max_samples),
        "random_state": int(random_state),
        "scaler_pickle_b64": _pickle_to_base64(scaler),
        "model_pickle_b64": _pickle_to_base64(model),
        "calibration": {
            "p50": round(float(np.nanpercentile(raw_scores, 50)), 6),
            "p90": round(float(np.nanpercentile(raw_scores, 90)), 6),
            "p95": round(float(np.nanpercentile(raw_scores, 95)), 6),
            "p99": round(float(np.nanpercentile(raw_scores, 99)), 6),
        },
    }


def _sklearn_isolation_forest_score(features: dict, forest: dict) -> dict:
    if SklearnIsolationForest is None or RobustScaler is None:
        return {"score": 0.0, "raw_score": 0.0, "method": "sklearn_not_available"}
    try:
        keys = forest.get("feature_keys", FEATURE_KEYS)
        vector = _feature_vector(features, keys).reshape(1, -1)
        scaler = _pickle_from_base64(forest["scaler_pickle_b64"])
        model = _pickle_from_base64(forest["model_pickle_b64"])
        normalized = scaler.transform(vector)
        raw_score = float(-model.score_samples(normalized)[0])
    except Exception as exc:  # noqa: BLE001
        return {"score": 0.0, "raw_score": 0.0, "method": "sklearn_isolation_forest_error", "error": str(exc)}

    calibration = forest.get("calibration", {})
    score_profile = _calibrated_anomaly_profile(raw_score, calibration)
    return {
        "score": score_profile["score"],
        "raw_score": round(raw_score, 6),
        "method": "sklearn_isolation_forest",
        "sklearn_version": forest.get("sklearn_version"),
        "calibration": calibration,
        "percentile": score_profile["percentile"],
        "band": score_profile["band"],
        "band_label": score_profile["band_label"],
    }


def _calibrated_anomaly_profile(raw_score: float, calibration: dict) -> dict:
    p50 = float(calibration.get("p50", 0.45))
    p90 = float(calibration.get("p90", p50))
    p95 = float(calibration.get("p95", p90))
    p99 = float(calibration.get("p99", p95))
    points = [(0.0, 0.0), (p50, 50.0), (p90, 90.0), (p95, 95.0), (p99, 99.0)]
    percentile = 99.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if raw_score <= x1:
            if abs(x1 - x0) < 1e-9:
                percentile = y1
            else:
                percentile = y0 + (raw_score - x0) * (y1 - y0) / (x1 - x0)
            break
    else:
        percentile = 99.0 + min(1.0, (raw_score - p99) / max(1e-6, p99 - p95))

    score_points = [(0.0, 0.0), (50.0, 0.05), (90.0, 0.30), (95.0, 0.60), (99.0, 0.85), (100.0, 1.0)]
    score = 0.0
    for (x0, y0), (x1, y1) in zip(score_points, score_points[1:]):
        if percentile <= x1:
            score = y0 + (percentile - x0) * (y1 - y0) / max(1e-6, x1 - x0)
            break
    else:
        score = 1.0

    if raw_score >= p99 or score >= 0.85:
        band, label = "strong", "强异常"
    elif raw_score >= p95 or score >= 0.60:
        band, label = "obvious", "明显异常"
    elif raw_score >= p90 or score >= 0.30:
        band, label = "mild", "轻微异常"
    else:
        band, label = "normal", "正常范围"

    return {
        "score": round(score, 4),
        "percentile": round(float(np.clip(percentile, 0.0, 100.0)), 1),
        "band": band,
        "band_label": label,
    }


def _knn_distance_profile(
    vector: np.ndarray,
    samples: np.ndarray,
    center: np.ndarray,
    scale: np.ndarray,
    *,
    k: int = 5,
    calibration: dict | None = None,
) -> dict:
    if samples.size == 0:
        return {
            "score": 0.0,
            "raw_score": 0.0,
            "method": "no_knn_samples",
            "nearest_count": 0,
            "band": "normal",
            "band_label": "正常范围",
        }

    normalized_samples = (samples - center) / scale
    normalized_vector = (vector - center) / scale
    nearest_count = max(1, min(int(k), len(normalized_samples)))
    distances = np.linalg.norm(normalized_samples - normalized_vector, axis=1)
    nearest = np.sort(distances)[:nearest_count]
    raw_score = float(np.mean(nearest))

    calibration = calibration or _knn_calibration(normalized_samples, nearest_count)
    score_profile = _calibrated_anomaly_profile(raw_score, calibration)
    return {
        "score": score_profile["score"],
        "raw_score": round(raw_score, 6),
        "method": "knn_distance",
        "nearest_count": int(nearest_count),
        "calibration": calibration,
        "percentile": score_profile["percentile"],
        "band": score_profile["band"],
        "band_label": score_profile["band_label"],
    }


def _knn_calibration(normalized_samples: np.ndarray, k: int) -> dict:
    n = len(normalized_samples)
    if n <= 1:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}

    nearest_count = max(1, min(int(k), n - 1))
    raw_scores = []
    for i in range(n):
        distances = np.linalg.norm(normalized_samples - normalized_samples[i], axis=1)
        distances = np.sort(distances)[1 : nearest_count + 1]
        raw_scores.append(float(np.mean(distances)))
    arr = np.array(raw_scores, dtype=float)
    return {
        "p50": round(float(np.nanpercentile(arr, 50)), 6),
        "p90": round(float(np.nanpercentile(arr, 90)), 6),
        "p95": round(float(np.nanpercentile(arr, 95)), 6),
        "p99": round(float(np.nanpercentile(arr, 99)), 6),
    }


def build_preferred_isolation_forest(matrix: np.ndarray, *, feature_keys: Iterable[str] = FEATURE_KEYS) -> dict:
    sklearn_forest = _build_sklearn_isolation_forest(matrix, feature_keys=feature_keys)
    if sklearn_forest:
        return sklearn_forest
    return build_isolation_forest(matrix, feature_keys=feature_keys)


def _lightweight_isolation_forest_score(features: dict, baseline: dict, forest: dict) -> dict:
    keys = forest.get("feature_keys", baseline.get("feature_keys", FEATURE_KEYS))
    vector = _feature_vector(features, keys)
    center = np.array(forest.get("center", []), dtype=float)
    scale = np.array(forest.get("scale", []), dtype=float)
    if len(center) != len(vector) or len(scale) != len(vector):
        return {"score": 0.0, "raw_score": 0.0, "method": "invalid_isolation_forest"}

    normalized = (vector - center) / scale
    raw_score = _isolation_raw_score(normalized, forest)
    calibration = forest.get("calibration", {})
    score_profile = _calibrated_anomaly_profile(raw_score, calibration)
    return {
        "score": score_profile["score"],
        "raw_score": round(float(raw_score), 6),
        "method": forest.get("method", "lightweight_isolation_forest"),
        "calibration": calibration,
        "percentile": score_profile["percentile"],
        "band": score_profile["band"],
        "band_label": score_profile["band_label"],
    }


def isolation_forest_score(features: dict, baseline: dict) -> dict:
    forest = baseline.get("isolation_forest") or {}
    if not forest:
        return {"score": 0.0, "raw_score": 0.0, "method": "no_isolation_forest"}

    if forest.get("method") == "sklearn_isolation_forest":
        result = _sklearn_isolation_forest_score(features, forest)
        if result.get("method") == "sklearn_isolation_forest":
            return result

        fallback = forest.get("lightweight_fallback") or baseline.get("lightweight_isolation_forest")
        if fallback:
            fallback_result = _lightweight_isolation_forest_score(features, baseline, fallback)
            fallback_result["method"] = "lightweight_isolation_forest_fallback"
            fallback_result["fallback_reason"] = result.get("method")
            if result.get("error"):
                fallback_result["sklearn_error"] = result.get("error")
            return fallback_result

        samples = np.array((baseline.get("global") or {}).get("samples", []), dtype=float)
        if samples.size:
            fallback = build_isolation_forest(samples, feature_keys=baseline.get("feature_keys", FEATURE_KEYS))
            fallback_result = _lightweight_isolation_forest_score(features, baseline, fallback)
            fallback_result["method"] = "lightweight_isolation_forest_runtime_fallback"
            fallback_result["fallback_reason"] = result.get("method")
            if result.get("error"):
                fallback_result["sklearn_error"] = result.get("error")
            return fallback_result
        return result

    return _lightweight_isolation_forest_score(features, baseline, forest)


def build_baseline(daily_features: pd.DataFrame) -> dict:
    baseline = {
        "feature_keys": FEATURE_KEYS,
        "stations": {},
        "global": {},
    }
    if daily_features.empty:
        return baseline

    global_matrix = np.vstack([_feature_vector(row) for _, row in daily_features.iterrows()])
    center, scale = _robust_center_scale(global_matrix)
    baseline["global"] = {
        "center": center.round(6).tolist(),
        "scale": scale.round(6).tolist(),
        "samples": global_matrix.round(6).tolist(),
        "count": int(len(global_matrix)),
    }
    baseline["global"]["knn_calibration"] = _knn_calibration((global_matrix - center) / scale, k=5)
    baseline["lightweight_isolation_forest"] = build_isolation_forest(global_matrix, feature_keys=FEATURE_KEYS)
    baseline["isolation_forest"] = build_preferred_isolation_forest(global_matrix, feature_keys=FEATURE_KEYS)
    if baseline["isolation_forest"].get("method") == "sklearn_isolation_forest":
        baseline["isolation_forest"]["lightweight_fallback"] = baseline["lightweight_isolation_forest"]

    for station, group in daily_features.groupby("station", sort=True):
        matrix = np.vstack([_feature_vector(row) for _, row in group.iterrows()])
        s_center, s_scale = _robust_center_scale(matrix)
        baseline["stations"][station] = {
            "center": s_center.round(6).tolist(),
            "scale": s_scale.round(6).tolist(),
            "samples": matrix.round(6).tolist(),
            "count": int(len(matrix)),
            "knn_calibration": _knn_calibration((matrix - s_center) / s_scale, k=5),
            "mean_pressure": float(group["mean"].median()),
            "p05_pressure": float(group["p05"].median()),
            "p95_pressure": float(group["p95"].median()),
        }
    return baseline


def anomaly_score(features: dict, baseline: dict) -> dict:
    station = features.get("station")
    model = baseline.get("stations", {}).get(station) or baseline.get("global", {})
    if not model or not model.get("center"):
        return {"score": 0.0, "method": "no_baseline", "top_features": []}

    keys = baseline.get("feature_keys", FEATURE_KEYS)
    vector = _feature_vector(features, keys)
    center = np.array(model["center"], dtype=float)
    scale = np.array(model["scale"], dtype=float)
    z = np.abs((vector - center) / scale)
    robust_score = float(np.clip(np.nanmean(np.minimum(z, 6.0)) / 3.0, 0.0, 1.0))

    samples = np.array(model.get("samples", []), dtype=float)
    knn = _knn_distance_profile(vector, samples, center, scale, k=5, calibration=model.get("knn_calibration"))
    baseline_score = robust_score
    isolation = isolation_forest_score(features, baseline)
    if isolation.get("method") == "no_isolation_forest":
        score = float(np.clip(0.60 * baseline_score + 0.40 * float(knn.get("score", 0.0)), 0.0, 1.0))
    else:
        score = float(
            np.clip(
                0.40 * baseline_score
                + 0.30 * float(isolation.get("score", 0.0))
                + 0.30 * float(knn.get("score", 0.0)),
                0.0,
                1.0,
            )
        )
    top_idx = np.argsort(z)[-5:][::-1]
    top_features = [
        {
            "feature": keys[int(i)],
            "z": round(float(z[int(i)]), 3),
            "value": round(float(vector[int(i)]), 6),
            "baseline": round(float(center[int(i)]), 6),
        }
        for i in top_idx
    ]
    return {
        "score": round(score, 4),
        "baseline_score": round(baseline_score, 4),
        "isolation_forest": isolation,
        "knn": knn,
        "method": "station_baseline" if station in baseline.get("stations", {}) else "global_baseline",
        "top_features": top_features,
    }
