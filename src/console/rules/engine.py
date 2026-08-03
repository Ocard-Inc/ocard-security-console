"""規則引擎：對指定視窗右界評估所有啟用規則，產出 Finding 清單。

- 逐規則錯誤隔離：單條失敗記 log，不中斷其他規則
- 門檻 = max(靜態地板, 28 天同時段基線 stat×factor)
- entity 一律以 fingerprint 組合為去重鍵；原始 acc/ip 僅在記憶體內短暫存在
- 內部帳號的新來源事件自動升級 P1
- Allowlist（生效中、範圍與 endpoint 相符）來源的 finding 抑制並回報 Suppression
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from console.core import brands, masking, stores, timewin
from console.core.ch import query
from console.core.config import settings
from console.rules import baseline
from console.rules.model import Finding, Rule, Suppression
from console.store import allowlist, db

logger = logging.getLogger(__name__)

_DISPLAY = masking.DISPLAY_FUNCS

# 品牌／分店欄位：去重鍵維持編號（名稱會改，鍵不能跟著漂移 —— 改一次店名就會讓
# 同一個對象變成新事件，既有的 active 事件從此不再更新並在三個 tick 後被標成
# 「已恢復」），顯示則帶上名稱。裸編號在 Slack 通知裡沒有人認得出是哪一家。
BRAND_COLUMN = "_brand"
STORE_COLUMN = "_store"
_LABEL_FUNCS = {BRAND_COLUMN: brands.label, STORE_COLUMN: stores.label}
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
            name = _LABEL_FUNCS.get(field.col)
            labels.append(name(raw) if name else text)
    return f"{rule.id}|" + "|".join(keys), " · ".join(labels), internal


def _allowlist_hit(rule: Rule, row: dict,
                   index: allowlist.Index) -> allowlist.Entry | None:
    """這一列命中了哪一條 allowlist 例外（沒有就 None）。

    **只比對 entity 裡 `fp: "src"` 的欄位值。** 這裡曾經拿
    `entity_key.split("|")` 逐段比對，而 entity_key 的格式是
    `f"{rule.id}|" + "|".join(keys)` —— 於是任何一段字面相符就抑制：

    - 一筆 `source_ip='R01'` 的條目 → **整條 R01 失效**
    - 一筆等於某個帳號名的條目 → 該帳號在所有規則下失效
    - 一筆等於某個 route 或品牌編號的條目（`fp: null` 的欄位原樣進 keys）→ 同上

    UI 一旦讓人自由輸入，這就從理論問題變成一次打錯字就能關掉整條規則。

    沒有 `fp: src` 欄位的規則仍然可能被抑制 —— 只要它有 endpoint 維度
    （R02/R04/R06/R11）就能建「只對這條規則、只對這個端點」的例外。
    實測 `Api2/GetProfile` 同時觸發 R03（src + endpoint）與 R04（只有 endpoint），
    少了後者那條路徑，例外只能讓 R03 閉嘴而 R04 繼續叫。
    完全沒有對象維度的規則（R09 的字面常數 scope、R12 的資料來源名）
    由 `allowlist.allowlistable()` 判定為不適用，API 據此回報 ——
    否則使用者為 R09 建一條例外，畫面顯示「生效中」而它什麼都不做。
    """
    srcs = [row.get(f.col) for f in rule.entity if f.fp == "src"]
    endpoint = str(row.get("endpoint") or row.get("route2") or "")
    return allowlist.match(srcs, rule_id=rule.id, endpoint=endpoint, index=index)


def _suppression(rule: Rule, entry: allowlist.Entry, entity_key: str, label: str,
                 metric: float, threshold: float, start: str, end: str) -> Suppression:
    return Suppression(
        rule_id=rule.id, rule_name=rule.name,
        allowlist_id=entry.id, allowlist_name=entry.name,
        source_ip=entry.source_ip, entity_key=entity_key, entity_label=label,
        metric=metric, threshold=threshold, window_start=start, window_end=end)


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


def _eval_sql_threshold(rule: Rule, start: str, end: str, end_dt: datetime,
                        allow: dict) -> tuple[list[Finding], list[Suppression]]:
    findings: list[Finding] = []
    suppressed: list[Suppression] = []
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
        entry = _allowlist_hit(rule, row, allow)
        if entry is not None:
            logger.info("%s %s 命中但在 Allowlist #%d 內，抑制", rule.id, label, entry.id)
            suppressed.append(_suppression(
                rule, entry, entity_key, label, metric, round(threshold, 1), start, end))
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
    return findings, suppressed


def _eval_new_source(rule: Rule, start: str, end: str, end_dt: datetime,
                     allow: dict) -> tuple[list[Finding], list[Suppression]]:
    findings: list[Finding] = []
    suppressed: list[Suppression] = []
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
        entity_key, label, internal = entity_parts(rule, row)

        # **allowlist 必須判在寫入 known_sources 之前。**
        # 反過來的話（原本的順序）被抑制的來源仍被記成「已知」，於是日後
        # 停用那條例外，R08A/B/C **也永遠不會再對它告警** —— 而畫面上
        # allowlist 是停用的、規則是啟用的，一切看起來正常。
        # known_sources 有 23 萬列、不在 _DERIVED_TABLES，那是這個功能唯一
        # 不可逆的資料汙染。
        #
        # 代價：例外到期後這個來源會以「新來源（90 天內首見）」的文案告警，
        # 而它其實已經活躍好幾個月。文案錯，但訊號在 —— 那比訊號永久消失好。
        # 另一個代價是每個 tick 都會為它留一列抑制紀錄（有保留期限，見
        # store/rule_suppressions.prune）。那是特性不是缺點：
        # 「這條例外每五分鐘遮掉一次東西」正是要看見的事。
        entry = _allowlist_hit(rule, row, allow)
        if entry is not None:
            logger.info("%s %s 首見但在 Allowlist #%d 內，抑制（不記入 known_sources）",
                        rule.id, label, entry.id)
            suppressed.append(_suppression(
                rule, entry, entity_key, label, metric, rule.min_events, start, end))
            continue

        with db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO known_sources (kind, entity_key, first_seen, origin)"
                " VALUES (?, ?, ?, 'live')",
                (rule.known_kind, fp_key, now_str))
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
    return findings, suppressed


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


def evaluate(rules: tuple[Rule, ...],
             window_end: datetime) -> tuple[list[Finding], list[str], list[Suppression]]:
    """評估全部規則。回傳 (findings, 失敗規則 id, 被 allowlist 抑制的紀錄)。

    呼叫端要餵 `rules.effective.effective_rules()`（含參數覆寫），不是
    `loader.load_rules()`。落盤由 store 層負責 —— 這裡只算，不寫 events
    也不寫 rule_suppressions（`_eval_new_source` 的 known_sources 是例外，
    那是判定本身的一部分）。
    """
    findings: list[Finding] = []
    failures: list[str] = []
    suppressed: list[Suppression] = []
    allow = allowlist.build_index(allowlist.active_entries())
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.off_hours_only and timewin.is_business_hours(window_end):
            continue
        start_dt = window_end - timedelta(minutes=rule.window_minutes)
        start, end = timewin.fmt(start_dt), timewin.fmt(window_end)
        # 逐規則的 try 必須包住覆寫套用之後的**全部**評估工作。
        # 逃到 run_tick 的例外會讓心跳那一列完全沒被更新（見 checker/tick.py），
        # 在 try 之內的話壞掉的規則只會進 failures、心跳帶出橘燈。
        try:
            if rule.kind == "sql_threshold":
                f, s = _eval_sql_threshold(rule, start, end, window_end, allow)
            elif rule.kind == "new_source":
                f, s = _eval_new_source(rule, start, end, window_end, allow)
            elif rule.kind == "freshness":
                f, s = _eval_freshness(rule, window_end), []
            else:
                continue
            findings.extend(f)
            suppressed.extend(s)
        except Exception:
            logger.exception("規則 %s 評估失敗", rule.id)
            failures.append(rule.id)
    return findings, failures, suppressed
