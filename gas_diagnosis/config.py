"""Default thresholds derived from the supplied PPT examples.

The first version intentionally keeps these values editable and visible because
they are business rules, not hidden model weights.
"""

from __future__ import annotations


DEFAULT_THRESHOLDS = {
    "low_pressure_kpa": 2.0,
    "high_warning_kpa": 2.75,
    "high_near_release_kpa": 3.0,
    "high_release_kpa": 3.1,
    "sensor_zero_kpa": 0.05,
    "sensor_spike_kpa": 6.0,
    "wave_min_amplitude_kpa": 0.04,
    "wave_abnormal_interval_min": 20.0,
    "drift_warning_kpa": 0.05,
    "night_start_hour": 0,
    "night_end_hour": 5,
    "morning_peak_start_hour": 6,
    "morning_peak_end_hour": 9,
    "evening_peak_start_hour": 17,
    "evening_peak_end_hour": 21,
}


FEATURE_KEYS = [
    "mean",
    "std",
    "min",
    "max",
    "p05",
    "p95",
    "range",
    "high_275_ratio",
    "high_300_ratio",
    "low_200_ratio",
    "night_mean",
    "night_max",
    "morning_min",
    "evening_min",
    "wave_count",
    "wave_interval_min",
    "slope_per_day",
]


HEALTH_LEVELS = {
    1: "优良",
    2: "健康",
    3: "亚健康",
    4: "风险",
    5: "高风险",
}
