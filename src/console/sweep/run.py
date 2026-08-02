"""併發執行探針，產出已遮罩的命中清單。

## 為什麼 executor 是模組層級的

`core/ch.py` 的 client 是 thread-local **且沒有回收機制** —— 每條新執行緒第一次查詢
會建一條連線，之後永不關閉（那是刻意的：clickhouse-connect 同 session 不可並行，
但每次呼叫新建又會洩漏 socket）。因此每跑一次掃描就 `ThreadPoolExecutor(...)`
會累積連線直到打爆伺服器的併發上限，正是 `ch.py` docstring 說要避免的事。

模組層級、有上限、跨掃描重用的 executor 讓工作執行緒數量固定，每條的 client
只建一次。代價是這些執行緒與連線在 process 生命週期內常駐 —— 對單一 process
的服務來說這是正確的取捨。

## 錯誤隔離

逐支探針隔離：單支失敗記 log 並列入 `failures`，不中斷其他探針。理由同
`rules/engine.evaluate()` —— 一支探針的 SQL 壞掉不該讓整份報告產不出來，
但**必須讓使用者看到少了哪支**，否則就是靜靜地少偵測。
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from console.core import masking, timewin
from console.core.config import settings
from console.core.ch import query
from console.sweep.probes import PER_DAY, Probe, probes

logger = logging.getLogger(__name__)

# 上限刻意小：ClickHouse 端也有併發限制，而探針數量本來就只有十來支，
# 再多也只是排隊。實測 7 支併發跑 30 天 = 10.6 秒（= 最慢那支的時間）。
MAX_WORKERS = 6

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="sweep")

# evidence 裡不呈現的欄位：
#   entity  原始識別值，只能以 fingerprint 離開探針層
#   metric  已是 Hit 的一級欄位
#   ips     原始 IP 陣列（P08 的 row_filter 判定用），**絕不可進 evidence** ——
#           它會被落盤、進 API 回應、送給 LLM。row_filter 回傳的是計數與業者名。
_DROP_COLUMNS = {"entity", "metric", "ips"}


@dataclass(frozen=True)
class Hit:
    """單一探針對單一對象的命中。所有欄位皆已遮罩，可直接落盤或送出 process。"""
    probe_id: str
    probe_name: str
    probe_summary: str
    signal_group: str
    entity_fp: str
    entity_kind: str           # actor / src
    metric: float
    floor: float               # 命中時的靜態地板，score.py 用來算 severity
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeRun:
    hits: tuple[Hit, ...]
    timings_ms: dict[str, int]      # probe_id → 耗時
    failures: dict[str, str]        # probe_id → 錯誤訊息
    skipped: tuple[str, ...]        # 未執行的 probe_id（cost=high 未勾選、需 intel 但無資料）
    params: dict[str, str]


def range_days(start: datetime, end: datetime) -> float:
    """區間天數（至少 1，避免不足一天的區間把 per_day 地板算成 0）。"""
    return max((end - start).total_seconds() / 86400, 1.0)


def build_params(start: datetime, end: datetime) -> dict[str, str]:
    """探針的共用參數。每支探針另外拿到自己的 `floor`（見 effective_floor）。

    prev_start / seed_start 一律由 **start** 往回推，不是由 end ——
    基線必須完全落在區間之外。用區間自己算基線的話，使用者拉「只含攻擊那兩天」
    的區間時，median 就是攻擊本身的量，倍數變 1.x，異常靜靜消失。
    """
    cfg = settings()["baseline"]
    return {
        "start": timewin.fmt(start),
        "end": timewin.fmt(end),
        "prev_start": timewin.fmt(start - timedelta(days=cfg["window_days"])),
        "seed_start": timewin.fmt(start - timedelta(days=cfg["seed_days"])),
    }


def effective_floor(probe: Probe, days: float) -> float:
    """per_day 探針的地板隨區間長度縮放；absolute 的不動。

    見 probes.py 的模組說明：少了這個縮放，「拉長區間」等於「悄悄降低門檻」。
    """
    if probe.floor_kind == PER_DAY:
        return round(probe.floor * days, 2)
    return probe.floor


def _clean(value: object) -> object:
    """pandas / numpy 值 → 可 JSON 序列化的 Python 值。"""
    if value is None:
        return None
    if isinstance(value, (bool, str)):
        return value
    # numpy 整數/浮點都有 item()；NaN 一律轉 None（下游要能分辨「沒有基線」與「基線是 0」）
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return masking.scrub_text(value, max_len=80)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    if isinstance(value, int):
        return value
    return masking.scrub_text(value, max_len=80)


def _run_one(probe: Probe, params: dict[str, str], floor: float) -> list[Hit]:
    fp = masking.DISPLAY_FUNCS[probe.fp_kind]
    df = query(probe.sql, {**params, "floor": floor})
    hits: list[Hit] = []
    for _, row in df.iterrows():
        entity_fp = fp(row["entity"])
        if not entity_fp:
            continue        # 空的識別值無法歸因，跳過而不是產生一個 "（空）" 對象
        extra: dict = {}
        if probe.row_filter is not None:
            # 逐列後處理（來源型態判定）。在這裡執行是因為它需要原始值，
            # 而原始值到下一行就只剩 fingerprint 了。回 None 表示這列不成立。
            result = probe.row_filter(row["entity"], dict(row))
            if result is None:
                continue
            extra = {k: _clean(v) for k, v in result.items()}
        evidence = {k: _clean(v) for k, v in row.items() if k not in _DROP_COLUMNS}
        evidence.update(extra)
        hits.append(Hit(
            probe_id=probe.id, probe_name=probe.name, probe_summary=probe.summary,
            signal_group=probe.signal_group,
            entity_fp=entity_fp, entity_kind=probe.entity_kind,
            metric=float(row["metric"]), floor=floor,
            evidence=evidence,
        ))
    return hits


def run_probes(
    start: datetime,
    end: datetime,
    *,
    include_high_cost: bool = False,
    intel_available: bool = False,
) -> ProbeRun:
    params = build_params(start, end)
    days = range_days(start, end)
    selected: list[Probe] = []
    skipped: list[str] = []
    for p in probes():
        if p.cost == "high" and not include_high_cost:
            skipped.append(p.id)
        elif p.needs_intel and not intel_available:
            skipped.append(p.id)
        else:
            selected.append(p)

    hits: list[Hit] = []
    timings: dict[str, int] = {}
    failures: dict[str, str] = {}

    def timed(probe: Probe) -> tuple[Probe, list[Hit] | None, int, str]:
        t0 = timewin.taipei_now()
        try:
            result = _run_one(probe, params, effective_floor(probe, days))
            err = ""
        except Exception as exc:  # noqa: BLE001 - 逐支隔離，見模組說明
            logger.exception("探針 %s 執行失敗", probe.id)
            result, err = None, f"{type(exc).__name__}: {exc}"
        ms = int((timewin.taipei_now() - t0).total_seconds() * 1000)
        return probe, result, ms, err

    for probe, result, ms, err in _executor.map(timed, selected):
        timings[probe.id] = ms
        if result is None:
            failures[probe.id] = err
        else:
            hits.extend(result)

    logger.info("掃描探針完成：%d 支、%d 命中、失敗 %s、跳過 %s",
                len(selected), len(hits), failures or "無", skipped or "無")
    return ProbeRun(hits=tuple(hits), timings_ms=timings, failures=failures,
                    skipped=tuple(skipped), params=params)
