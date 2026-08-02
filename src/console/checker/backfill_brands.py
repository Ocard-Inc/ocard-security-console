"""補算既有事件的「涉及品牌」明細。

品牌明細功能上線前建立的事件，context 裡沒有 brand_top；已 resolved 的事件不會
再被 tick 更新，畫面上就只剩一個無法展開的數字。這支指令重跑該事件**最後一個命中
視窗**的規則 SQL，把同一個對象的逐品牌次數寫回去。

側效應僅限 events.context_json：
- 只讀 ClickHouse 與 MySQL
- 不碰 known_sources（所以不用 engine.evaluate，直接跑 SQL）
- 不送通知、不改 metric/threshold/status 等任何判定欄位

品牌數對不上就跳過不寫 —— 與其寫入一份和畫面數字矛盾的明細，不如維持現狀並回報。

    uv run python -m console.checker.backfill_brands             # 只補缺的
    uv run python -m console.checker.backfill_brands --all       # 全部重算
    uv run python -m console.checker.backfill_brands --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import timedelta

from console.core import brands, timewin
from console.core.ch import ChQueryError, query
from console.core.logging_setup import setup_logging
from console.queries import exprs
from console.rules import engine
from console.rules.loader import load_rules
from console.rules.model import Rule
from console.store import db

logger = logging.getLogger(__name__)


def _pending(refresh_all: bool) -> list[dict]:
    rows = db.rows("SELECT * FROM events WHERE brands IS NOT NULL AND brands > 0"
                   " ORDER BY evt_no")
    out = []
    for row in rows:
        ctx = json.loads(row["context_json"] or "{}")
        if refresh_all or not ctx.get("brand_top"):
            out.append(row)
    return out


def _rule_of(event: dict, rules: tuple[Rule, ...]) -> Rule | None:
    return next((r for r in rules if r.id == event["rule_id"]), None)


def _windows(event: dict, rule: Rule) -> list[tuple[str, str, str]]:
    """context 可能來自哪個視窗。

    本次改動前，`brands` 與 context 只在事件建立時寫入，`last_seen` 卻每個 tick
    都更新 —— 兩者未必同一個視窗。因此兩個候選都試，取能重現已記錄品牌數的那個。
    """
    span = timedelta(minutes=rule.window_minutes)
    created = timewin.parse(event["first_seen"])
    last_end = timewin.parse(event["last_seen"])
    out = [("建立視窗", created, created + span)]
    if last_end - span != created:
        out.append(("最後命中視窗", last_end - span, last_end))
    return [(name, timewin.fmt(s), timewin.fmt(e)) for name, s, e in out]


def _brand_top(event: dict, rule: Rule) -> tuple[list[dict] | None, str]:
    """回傳 (brand_top, 說明)。取不到時 brand_top 為 None，不寫入。"""
    if not rule.sql or exprs.BRAND_MAP not in rule.sql:
        return None, "規則 SQL 沒有逐品牌次數"
    tried = []
    for name, start, end in _windows(event, rule):
        try:
            df = query(rule.sql, {"start": start, "end": end})
        except ChQueryError as exc:
            return None, f"查詢失敗：{exc}"
        matched = None
        for _, raw in df.iterrows():
            row = {k: (None if isinstance(v, float) and math.isnan(v) else v)
                   for k, v in raw.items()}
            if engine.entity_parts(rule, row)[0] == event["entity_key"]:
                matched = row
                break
        if matched is None:
            tried.append(f"{name}查不到這個對象")
            continue
        found = int(matched["brands"]) if matched.get("brands") is not None else None
        if found != event["brands"]:
            # 數字對不上就換下一個視窗；寧可不補，也不寫入與畫面矛盾的明細
            tried.append(f"{name}品牌數為 {found}")
            continue
        top = brands.breakdown(matched.get(engine.BRAND_MAP_COLUMN))
        if not top:
            tried.append(f"{name}沒有品牌分布")
            continue
        return top, f"{name} {start[11:16]}–{end[11:16]}，{len(top)} 個品牌"
    return None, f"找不到品牌數為 {event['brands']} 的視窗（{'、'.join(tried)}）"


def backfill(refresh_all: bool = False, dry_run: bool = False) -> dict:
    rules = load_rules()
    events = _pending(refresh_all)
    updated, skipped = 0, []
    for event in events:
        rule = _rule_of(event, rules)
        if rule is None:
            skipped.append((event["evt_no"], f"找不到規則 {event['rule_id']}"))
            continue
        top, note = _brand_top(event, rule)
        if top is None:
            skipped.append((event["evt_no"], note))
            continue
        print(f"{event['evt_no']} {event['rule_id']} {event['entity_label']}｜{note}"
              + "".join(f"\n    {b['label']} {b['count']:,} 次" for b in top))
        if dry_run:
            continue
        ctx = json.loads(event["context_json"] or "{}")
        ctx["brand_top"] = top
        with db.tx() as conn:
            conn.execute("UPDATE events SET context_json = ? WHERE id = ?",
                         (json.dumps(ctx, ensure_ascii=False, default=str), event["id"]))
        updated += 1

    print(f"\n=== 待補 {len(events)} 件；"
          f"{'（dry-run，未寫入）' if dry_run else f'已更新 {updated} 件'}"
          f"；跳過 {len(skipped)} 件 ===")
    for evt_no, why in skipped:
        print(f"  {evt_no}：{why}")
    return {"pending": len(events), "updated": updated, "skipped": len(skipped)}


def main() -> None:
    parser = argparse.ArgumentParser(description="補算既有事件的涉及品牌明細")
    parser.add_argument("--all", action="store_true",
                        help="連已有明細的事件也重算（例如當初 MySQL 不可用）")
    parser.add_argument("--dry-run", action="store_true", help="只顯示結果，不寫入")
    args = parser.parse_args()
    setup_logging()
    backfill(refresh_all=args.all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
