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
    # SQL 裡 `HAVING metric >= N` 的字面值（loader 從 sql 解析，抓不到就是 None）。
    #
    # 這是**門檻的真正下限**：ClickHouse 端就先濾掉低於它的列了，所以把
    # static_floor 調到它以下不會讓規則更靈敏 —— UI 顯示新值、events.threshold
    # 記新值、命中數完全不變。使用者的結論會是「調低門檻也沒有更多告警，
    # 所以真的沒事」。這個欄位存在的唯一理由就是讓那件事說得出來。
    sql_floor: float | None = None


@dataclass(frozen=True)
class Suppression:
    """一次「命中了規則，但被 allowlist 擋掉」的紀錄。

    抑制原本是整筆丟棄、只在 log 留一行（new_source 連 log 都沒有），
    於是沒有人看得出這個刻意製造的盲區實際遮掉了多少東西 ——
    而那正是判斷一條例外該不該續期的唯一依據。
    """
    rule_id: str
    rule_name: str
    allowlist_id: int
    allowlist_name: str
    source_ip: str
    # 與 Finding 相同的去重鍵。store/events.py 靠它認出「這個 active 事件本來
    # 會命中，只是被抑制了」，因此不該被當成「已恢復」。
    entity_key: str
    entity_label: str
    metric: float
    threshold: float
    window_start: str
    window_end: str


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
