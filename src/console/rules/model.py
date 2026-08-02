"""規則與 Finding 的 frozen dataclass 模型。"""
from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("P0", "P1", "P2", "P3")
RULE_KINDS = ("sql_threshold", "new_source", "freshness")


@dataclass(frozen=True)
class EntityField:
    col: str                 # SQL 輸出欄位名
    fp: str | None = None    # 遮罩種類：actor/src/token/resource；None = 原樣呈現（route、endpoint、brand）


@dataclass(frozen=True)
class Threshold:
    static_floor: float
    baseline_key: str | None = None   # 可含 {col} 樣板，例 api_endpoint_60m:{endpoint}
    stat: str = "p95"                 # median/p95/p99
    factor: float = 2.0
    # population=True 表示基線是「跨實體的分布」（例如所有來源各自的 60 分鐘量），
    # 而非「這個實體自己的歷史」。此時拿實體量除以分布 median 沒有意義
    # （大來源除以典型來源會得到上千倍的誤導數字），因此不計算倍數，
    # 只呈現門檻與分位語意。
    population: bool = False


@dataclass(frozen=True)
class RatioGuard:
    den_col: str          # 分母欄位（如 total）
    min_ratio: float      # metric/den >= min_ratio 才成立


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str
    source: str                       # admin/backend/api/auth/all
    kind: str
    window_minutes: int
    enabled: bool
    sql: str | None = None            # sql_threshold / new_source 用；含 %(start)s %(end)s
    threshold: Threshold | None = None
    entity: tuple[EntityField, ...] = field(default_factory=tuple)
    off_hours_only: bool = False
    ratio: RatioGuard | None = None
    known_kind: str | None = None     # new_source：known_sources.kind
    min_events: float = 0             # new_source：視窗內至少 N 筆才視為有意義的新來源
    cooldown_minutes: int = 60
    note: str = ""


@dataclass(frozen=True)
class Finding:
    rule: Rule
    entity_key: str                   # 去重鍵（fingerprint 組合，含 rule id）
    entity_label: str                 # 顯示用（已遮罩）
    metric: float
    threshold: float
    baseline_median: float | None
    baseline_p95: float | None
    multiple: float | None            # metric / median
    brands: int | None
    window_start: str
    window_end: str
    severity: str                     # 可能被引擎升級（內部帳號）
    context: dict = field(default_factory=dict)   # 已遮罩
