"""基線讀取與動態門檻：28 天同時段 median / P95 / P99 / max。

寫入端在 checker/calibrate.py（每日 06:00 重算）；本模組提供查詢與
「靜態地板與基線動態線取 max」的門檻計算。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from console.store import db


@dataclass(frozen=True)
class Baseline:
    median: float
    p95: float
    p99: float
    maxv: float
    samples: int
    generated_at: str


def day_class_of(dt: datetime) -> str:
    return "weekend" if dt.weekday() >= 5 else "weekday"


def get(metric_key: str, hour: int = -1, day_class: str = "all") -> Baseline | None:
    """讀基線；找不到精確 (hour, day_class) 時逐層回退到全域分布。"""
    candidates = [
        (hour, day_class),
        (hour, "all"),
        (-1, day_class),
        (-1, "all"),
    ]
    for h, dc in candidates:
        row = db.one(
            "SELECT median, p95, p99, maxv, samples, generated_at FROM baselines"
            " WHERE metric_key = ? AND hour = ? AND day_class = ?",
            (metric_key, h, dc),
        )
        if row and row["samples"]:
            return Baseline(
                median=row["median"] or 0.0,
                p95=row["p95"] or 0.0,
                p99=row["p99"] or 0.0,
                maxv=row["maxv"] or 0.0,
                samples=row["samples"],
                generated_at=row["generated_at"],
            )
    return None


def threshold(
    metric_key: str,
    *,
    at: datetime,
    static_floor: float,
    mode: str = "p95x2",
) -> tuple[float, Baseline | None]:
    """回傳 (實際門檻, 基線)。門檻 = max(靜態地板, 基線動態線)。

    mode: p95x2 | p99x2 | medianx8
    """
    base = get(metric_key, hour=at.hour, day_class=day_class_of(at))
    dynamic = 0.0
    if base is not None:
        if mode == "p95x2":
            dynamic = base.p95 * 2
        elif mode == "p99x2":
            dynamic = base.p99 * 2
        elif mode == "medianx8":
            dynamic = base.median * 8
        else:
            raise ValueError(f"未知門檻模式 {mode!r}")
    return max(static_floor, dynamic), base


def upsert_many(rows: list[tuple], generated_at: str) -> int:
    """rows: (metric_key, hour, day_class, median, p95, p99, maxv, samples)"""
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO baselines"
            " (metric_key, hour, day_class, median, p95, p99, maxv, samples, generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, generated_at) for r in rows],
        )
    return len(rows)


def age_days(now: datetime) -> float | None:
    """最舊 metric 的基線年齡（天）；無基線回 None。"""
    row = db.one("SELECT min(generated_at) AS oldest FROM baselines")
    if not row or not row["oldest"]:
        return None
    oldest = datetime.strptime(row["oldest"], "%Y-%m-%d %H:%M:%S")
    return (now - oldest).total_seconds() / 86400
