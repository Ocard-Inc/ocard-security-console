"""基線讀取與動態門檻：28 天同時段 median / P95 / P99 / max。

寫入端在 checker/calibrate.py（每日 06:00 重算）；本模組提供查詢與
「靜態地板與基線動態線取 max」的門檻計算。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

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


def window_point(window_end: datetime, window_minutes: float) -> datetime:
    """回傳「代表 `[window_end - window_minutes, window_end)` 這個視窗」的時刻。

    **基線的桶是 `toStartOfHour(create_time)`**（calibrate.py），所以要拿一個視窗
    的量去比基線，必須先決定它算哪一小時。答案是**視窗中點**，不是 window_end ——
    後者對任何往回看的視窗都是錯的，而且每小時必有一個 tick 的 window_end 落在
    整點，那時 60 分鐘視窗的內容剛好是**前一個**整點小時、卻查到**下一個**小時。

    2026-08-06 正式環境實測：傍晚流量逐小時陡降，於是 21:00~22:00 的正常量
    （`Api2/GivePoint` 2,117，前七天同時段 1,768~2,097）拿 h22 的門檻 2,000 來比
    就命中了；h21 自己的門檻是 5,083。20 筆 R04 事件裡 19 筆是這樣來的。
    反方向（早上流量上升）門檻被抬高，`Api2/GetProfile` 在 h07 要 9.22 倍才叫，
    設計上是 3.04 倍 —— **規則靜靜漏抓，那半邊沒有任何告警可以提醒人**。

    為什麼是中點而不是視窗起點：起點在 window_end 不落整點時會反過來錯。
    22:40 的 tick 視窗是 `[21:40, 22:40)`，40 分鐘在 h22 —— 起點會給 h21。
    中點對 60 分鐘以內的視窗永遠落在涵蓋分鐘數最多的那一小時
    （`tests/test_baseline_window_hour.py` 用逐分鐘的獨立 oracle 驗證）。

    day_class 也一起修正：跨午夜的視窗（週一 00:00 的 tick 看的是週日 23 點）
    原本會拿 weekday 的母體去比週末的量。
    """
    return window_end - timedelta(minutes=window_minutes / 2)


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
