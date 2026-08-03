"""規則 YAML 載入與驗證。

驗證項目：id 唯一、severity/kind/source 合法、SQL 僅 SELECT/WITH 且不含分號、
只引用允許的表、entity 欄位/fp 種類合法、數值為正。
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

from console.core.config import CONFIG_DIR, settings
from console.rules.model import (
    RULE_KINDS, SEVERITIES, EntityField, RatioGuard, Rule, Threshold,
)

logger = logging.getLogger(__name__)

RULES_DIR = CONFIG_DIR / "rules"

_FP_KINDS = {None, "actor", "src", "token", "resource"}
# 排除函式呼叫（如 trim(BOTH ' ' FROM splitByChar(...))）與子查詢 FROM (
_TABLE_RE = re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)\b(?!\s*\()", re.IGNORECASE)
# SQL 端的預篩門檻。15 條有 SQL 的規則全部寫成 `HAVING metric >= N`（字面值）。
# 抓不到就留 None 而不是猜一個數字 —— 見 Rule.sql_floor 的說明。
_SQL_FLOOR_RE = re.compile(r"\bHAVING\s+metric\s*>=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


class RuleConfigError(ValueError):
    pass


def _allowed_tables() -> set[str]:
    return {s["table"] for s in settings()["data_sources"].values()}


def _validate_sql(rule_id: str, sql: str) -> None:
    head = sql.lstrip().lower()
    if not head.startswith(("select", "with")):
        raise RuleConfigError(f"{rule_id}: SQL 必須以 SELECT/WITH 開頭")
    if ";" in sql:
        raise RuleConfigError(f"{rule_id}: SQL 不可含分號")
    if "%(start)s" not in sql or "%(end)s" not in sql:
        raise RuleConfigError(f"{rule_id}: SQL 必須帶 %(start)s / %(end)s 時間參數")
    allowed = _allowed_tables()
    for table in _TABLE_RE.findall(sql):
        name = table.split(".")[-1]
        if name not in allowed and not name.startswith("("):
            raise RuleConfigError(
                f"{rule_id}: SQL 引用了白名單外的表 {table!r}（允許：{sorted(allowed)}）")


def _parse_rule(path: Path, data: dict) -> Rule:
    rid = str(data.get("id") or "").strip()
    if not rid:
        raise RuleConfigError(f"{path.name}: 缺少 id")
    if data.get("severity") not in SEVERITIES:
        raise RuleConfigError(f"{rid}: severity 必須是 {SEVERITIES}")
    kind = data.get("kind", "sql_threshold")
    if kind not in RULE_KINDS:
        raise RuleConfigError(f"{rid}: kind 必須是 {RULE_KINDS}")

    sql = data.get("sql")
    if kind in ("sql_threshold", "new_source"):
        if not sql:
            raise RuleConfigError(f"{rid}: kind={kind} 必須提供 sql")
        _validate_sql(rid, sql)

    threshold = None
    if kind == "sql_threshold":
        t = data.get("threshold") or {}
        floor = float(t.get("static_floor", 0))
        if floor <= 0:
            raise RuleConfigError(f"{rid}: threshold.static_floor 必須為正數")
        stat = t.get("stat", "p95")
        if stat not in ("median", "p95", "p99"):
            raise RuleConfigError(f"{rid}: threshold.stat 必須是 median/p95/p99")
        threshold = Threshold(
            static_floor=floor,
            baseline_key=t.get("baseline_key"),
            stat=stat,
            factor=float(t.get("factor", 2.0)),
            population=bool(t.get("population", False)),
        )

    entity = []
    for e in data.get("entity", []):
        fp = e.get("fp")
        if fp not in _FP_KINDS:
            raise RuleConfigError(f"{rid}: entity.fp 必須是 {_FP_KINDS}")
        entity.append(EntityField(col=e["col"], fp=fp))
    if kind in ("sql_threshold", "new_source") and not entity:
        raise RuleConfigError(f"{rid}: 必須至少定義一個 entity 欄位")

    ratio = None
    if data.get("ratio"):
        r = data["ratio"]
        ratio = RatioGuard(den_col=r["den_col"], min_ratio=float(r["min_ratio"]))

    if kind == "new_source" and not data.get("known_kind"):
        raise RuleConfigError(f"{rid}: kind=new_source 必須指定 known_kind")

    window = int(data.get("window_minutes", 10))
    if window <= 0 or window > 24 * 60:
        raise RuleConfigError(f"{rid}: window_minutes 必須在 1~1440")

    return Rule(
        id=rid,
        name=str(data.get("name") or rid),
        severity=data["severity"],
        source=data.get("source", "all"),
        kind=kind,
        window_minutes=window,
        enabled=bool(data.get("enabled", True)),
        sql=sql,
        threshold=threshold,
        entity=tuple(entity),
        off_hours_only=bool(data.get("off_hours_only", False)),
        ratio=ratio,
        known_kind=data.get("known_kind"),
        min_events=float(data.get("min_events", 0)),
        cooldown_minutes=int(data.get("cooldown_minutes", 60)),
        note=str(data.get("note", "")),
        sql_floor=_sql_floor(sql),
    )


def _sql_floor(sql: str | None) -> float | None:
    m = _SQL_FLOOR_RE.search(sql) if sql else None
    return float(m.group(1)) if m else None


@lru_cache(maxsize=1)
def load_rules() -> tuple[Rule, ...]:
    """YAML 的真相。**不要 cache_clear()** —— 見 rules/effective.py。

    五分鐘檢查與回測用的不是這個函式，而是
    `rules.effective.effective_rules()`（YAML + SQLite 的參數覆寫）。
    這裡回傳的是「檔案裡寫了什麼」，供 API 並列顯示「原值 → 目前生效值」。
    """
    rules: list[Rule] = []
    seen: set[str] = set()
    for path in sorted(RULES_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise RuleConfigError(f"{path.name}: 頂層必須是 mapping")
        rule = _parse_rule(path, data)
        if rule.id in seen:
            raise RuleConfigError(f"{path.name}: 重複的規則 id {rule.id}")
        seen.add(rule.id)
        rules.append(rule)
    logger.info("載入 %d 條規則（啟用 %d）", len(rules), sum(r.enabled for r in rules))
    return tuple(rules)
