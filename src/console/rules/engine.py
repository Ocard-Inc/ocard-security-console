"""規則引擎：對指定視窗右界評估所有啟用規則，產出 Finding 清單。

- 逐規則錯誤隔離：單條失敗記 log，不中斷其他規則
- 門檻 = max(靜態地板, 28 天同時段基線 stat×factor)
- entity 一律以 fingerprint 組合為去重鍵；原始 acc/ip 僅在記憶體內短暫存在
- 內部帳號的新來源事件自動升級 P1
- Allowlist（生效中、endpoint 相符）來源的 finding 直接抑制
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from console.core import brands, masking, timewin
from console.core.ch import query
from console.core.config import settings
from console.rules import baseline
from console.rules.model import EntityField, Finding, Rule
from console.store import db

logger = logging.getLogger(__name__)

_DISPLAY = masking.DISPLAY_FUNCS

# 品牌欄位：去重鍵維持編號（名稱會改，鍵不能跟著漂移），顯示則帶上名稱
BRAND_COLUMN = "_brand"
# 規則 SQL 以 exprs.BRAND_MAP 產出的逐品牌次數（見 config/rules/*.yaml）
BRAND_MAP_COLUMN = "brand_map"


def _is_internal_account(raw: str) -> bool:
    cfg = settings()["internal_accounts"]
    value = str(raw)
    if value in set(cfg.get("accounts", [])):
        return True
    return any(value.startswith(p) for p in cfg.get("prefixes", []))


def entity_parts(rule: Rule, row: dict) -> tuple[str, str, bool]:
    """回傳 (entity_key, entity_label, has_internal_account)。"""
    keys, labels, internal = [], [], False
    for field in rule.entity:
        raw = row.get(field.col)
        if field.fp:
            fp = _DISPLAY[field.fp](raw)
            keys.append(fp or "-")
            labels.append(fp or "（空）")
            if field.fp == "actor" and raw is not None and _is_internal_account(raw):
                internal = True
        else:
            if raw is None:
                text = "（空）"
            elif isinstance(raw, float) and raw.is_integer():
                text = str(int(raw))
            else:
                text = str(raw)
            keys.append(text)
            labels.append(brands.label(raw) if field.col == BRAND_COLUMN else text)
    return f"{rule.id}|" + "|".join(keys), " · ".join(labels), internal


def _active_allowlist_srcs() -> dict[str, set[str]]:
    """生效中 allowlist：src → 允許的 endpoint 集合（空字串 = 全部）。"""
    now = timewin.fmt(timewin.taipei_now())
    rows = db.rows(
        "SELECT source_fp, COALESCE(endpoint, '') AS ep FROM allowlist"
        " WHERE status = '生效中'"
        " AND (valid_from IS NULL OR valid_from <= ?)"
        " AND (valid_to IS NULL OR valid_to >= ?)",
        (now, now),
    )
    result: dict[str, set[str]] = {}
    for r in rows:
        if r["source_fp"]:
            result.setdefault(r["source_fp"], set()).add(r["ep"])
    return result


def _is_allowlisted(entity_key: str, row: dict, allow: dict[str, set[str]]) -> bool:
    for part in entity_key.split("|"):
        if part in allow:
            eps = allow[part]
            endpoint = str(row.get("endpoint") or row.get("route2") or "")
            if "" in eps or endpoint in eps:
                return True
    return False


def _resolve_threshold(rule: Rule, row: dict, window_end: datetime):
    t = rule.threshold
    if t is None:
        return 0.0, None
    key = t.baseline_key
    if key and "{" in key:
        key = key.format(**{k: row.get(k) for k in row})
    base = baseline.get(key, hour=window_end.hour,
                        day_class=baseline.day_class_of(window_end)) if key else None
    dynamic = 0.0
    if base is not None:
        dynamic = getattr(base, t.stat) * t.factor
    return max(t.static_floor, dynamic), base


def _eval_sql_threshold(rule: Rule, start: str, end: str,
                        end_dt: datetime, allow: dict) -> list[Finding]:
    findings: list[Finding] = []
    df = query(rule.sql, {"start": start, "end": end})
    for _, row in df.iterrows():
        row = {k: (None if isinstance(v, float) and math.isnan(v) else v)
               for k, v in row.items()}
        metric = float(row["metric"])
        threshold, base = _resolve_threshold(rule, row, end_dt)
        if metric < threshold:
            continue
        if rule.ratio is not None:
            den = float(row.get(rule.ratio.den_col) or 0)
            if den <= 0 or metric / den < rule.ratio.min_ratio:
                continue
        entity_key, label, _ = entity_parts(rule, row)
        if _is_allowlisted(entity_key, row, allow):
            logger.info("%s %s 命中但在 Allowlist 內，抑制", rule.id, label)
            continue
        population = rule.threshold.population
        median = None if population else (base.median if base else None)
        context = _masked_context(rule, row)
        if population and base is not None:
            context["baseline_note"] = (
                f"基線為同時段所有同類對象的量分布（median {base.median:.0f}、"
                f"P95 {base.p95:.0f}、P99 {base.p99:.0f}），非此對象自身歷史；"
                "因此不計算「相對自身」的倍數，改以是否超出群體高分位判定。")
        findings.append(Finding(
            rule=rule, entity_key=entity_key, entity_label=label,
            metric=metric, threshold=round(threshold, 1),
            baseline_median=median,
            baseline_p95=None if population else (base.p95 if base else None),
            multiple=round(metric / median, 1) if median else None,
            brands=int(row["brands"]) if row.get("brands") is not None else None,
            window_start=start, window_end=end, severity=rule.severity,
            context=context,
        ))
    return findings


def _eval_new_source(rule: Rule, start: str, end: str,
                     end_dt: datetime, allow: dict) -> list[Finding]:
    findings: list[Finding] = []
    df = query(rule.sql, {"start": start, "end": end})
    now_str = timewin.fmt(timewin.taipei_now())
    for _, row in df.iterrows():
        row = dict(row)
        metric = float(row["metric"])
        if metric < rule.min_events:
            continue
        fp_key = "|".join(
            _DISPLAY[f.fp](row.get(f.col)) or "-" for f in rule.entity if f.fp)
        known = db.one(
            "SELECT 1 AS x FROM known_sources WHERE kind = ? AND entity_key = ?",
            (rule.known_kind, fp_key))
        if known:
            continue
        with db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO known_sources (kind, entity_key, first_seen, origin)"
                " VALUES (?, ?, ?, 'live')",
                (rule.known_kind, fp_key, now_str))
        entity_key, label, internal = entity_parts(rule, row)
        if _is_allowlisted(entity_key, row, allow):
            continue
        severity = "P1" if internal else rule.severity
        findings.append(Finding(
            rule=rule, entity_key=entity_key, entity_label=label,
            metric=metric, threshold=rule.min_events,
            baseline_median=None, baseline_p95=None, multiple=None,
            brands=int(row["brands"]) if row.get("brands") is not None else None,
            window_start=start, window_end=end, severity=severity,
            context={**_masked_context(rule, row),
                     "note": "內部帳號新來源" if internal else "新來源（90 天內首見）"},
        ))
    return findings


def _eval_freshness(rule: Rule, end_dt: datetime) -> list[Finding]:
    findings: list[Finding] = []
    cfg = settings()
    alert_min = cfg["freshness"]["alert_minutes"]
    lookback = timewin.fmt(end_dt - timedelta(hours=2))
    now = timewin.taipei_now()
    for key, src in cfg["data_sources"].items():
        df = query(
            f"SELECT max(create_time) AS mx FROM {src['table']}"
            f" WHERE create_time >= %(start)s", {"start": lookback})
        mx = df.iloc[0]["mx"]
        lag_min = (now - mx.to_pydatetime()).total_seconds() / 60 if mx is not None else 999
        if lag_min <= alert_min:
            continue
        findings.append(Finding(
            rule=rule, entity_key=f"{rule.id}|{key}", entity_label=src["label"],
            metric=round(lag_min, 1), threshold=float(alert_min),
            baseline_median=None, baseline_p95=None, multiple=None, brands=None,
            window_start=lookback, window_end=timewin.fmt(now), severity=rule.severity,
            context={"lag_minutes": round(lag_min, 1), "table": src["table"]},
        ))
    return findings


def _masked_context(rule: Rule, row: dict) -> dict:
    """row 轉為可持久化的已遮罩 context（entity 欄位以 fp 取代、其餘保留數值）。"""
    fp_cols = {f.col: f.fp for f in rule.entity}
    ctx: dict = {}
    for k, v in row.items():
        if v is None:
            continue
        if k == BRAND_MAP_COLUMN:
            # 展開「涉及品牌 N 個」用；事後重查 ClickHouse 成本高且視窗未必還在，
            # 因此在偵測當下就把前 N 名連同名稱一起存進事件 context。
            ctx["brand_top"] = brands.breakdown(v)
        elif k in fp_cols and fp_cols[k]:
            ctx[k] = _DISPLAY[fp_cols[k]](v)
        elif isinstance(v, (int, float)):
            ctx[k] = v
        else:
            ctx[k] = masking.scrub_text(v, max_len=120)
    return ctx


def evaluate(rules: tuple[Rule, ...], window_end: datetime) -> tuple[list[Finding], list[str]]:
    """評估全部規則。回傳 (findings, 失敗規則 id 清單)。"""
    findings: list[Finding] = []
    failures: list[str] = []
    allow = _active_allowlist_srcs()
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.off_hours_only and timewin.is_business_hours(window_end):
            continue
        start_dt = window_end - timedelta(minutes=rule.window_minutes)
        start, end = timewin.fmt(start_dt), timewin.fmt(window_end)
        try:
            if rule.kind == "sql_threshold":
                findings.extend(_eval_sql_threshold(rule, start, end, window_end, allow))
            elif rule.kind == "new_source":
                findings.extend(_eval_new_source(rule, start, end, window_end, allow))
            elif rule.kind == "freshness":
                findings.extend(_eval_freshness(rule, window_end))
        except Exception:
            logger.exception("規則 %s 評估失敗", rule.id)
            failures.append(rule.id)
    return findings, failures
