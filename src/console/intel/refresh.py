"""建立／更新 ip_intel：掃 ClickHouse 的相異來源 IP → 離線分類 → 寫入 SQLite。

## 落盤的是什麼

**只有 fingerprint 與分類結果**，沒有原始 IP。原始 IP 在這個 process 內短暫存在、
分類完就丟掉 —— 與 `checker/calibrate.seed_known_sources()` 的做法一致。
分類（機房／VPN／住宅）與歸屬（業者名、區域）不是個資，把它們跟 fingerprint
一起存不會讓系統獲得還原能力。

## CLI

    uv run python -m console.intel.refresh                    # 近 90 天
    uv run python -m console.intel.refresh --days 180
    uv run python -m console.intel.refresh --seed-allowlist    # 一併把我方出口播種進 allowlist
    uv run python -m console.intel.refresh --dry-run           # 只印統計，不寫入

每日排程（`checker/scheduler.run_daily`）會在基線重算後跑一次增量。
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import timedelta

from console.core import timewin
from console.core.ch import query
from console.core.logging_setup import setup_logging
from console.core import masking
from console.intel import classify, ranges
from console.queries import exprs
from console.store import db

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 90

# 四張表裡「來源 IP」的位置。admin/backend/auth 有 ip 欄位；
# api 的來源要從 headers 推導，成本高（3.4 億列解 JSON），預設不掃 ——
# 需要時用 --include-api。
_IP_SOURCES = (
    ("ods_backend_sys_log", "coalesce(ip, '')"),
    ("ods_admin_log", "ip"),
    ("ods_auth_log", "coalesce(ip, '')"),
)


def _distinct_ips(days: int, *, include_api: bool = False) -> list[str]:
    """近 N 天出現過的相異來源 IP（原始值，僅在記憶體內短暫存在）。"""
    end = timewin.effective_now()
    params = {"start": timewin.fmt(end - timedelta(days=days)), "end": timewin.fmt(end)}
    tf = exprs.time_filter()
    parts = [f"SELECT DISTINCT {expr} AS ip FROM {table} WHERE {tf}"
             for table, expr in _IP_SOURCES]
    if include_api:
        parts.append(
            f"SELECT DISTINCT {exprs.API_SRC_IP} AS ip FROM ods_api_log WHERE {tf}")
    sql = f"SELECT DISTINCT ip FROM ({' UNION ALL '.join(parts)}) WHERE ip != ''"
    df = query(sql, params)
    return [str(v) for v in df["ip"]]


def refresh(days: int = DEFAULT_DAYS, *, include_api: bool = False,
            dry_run: bool = False) -> dict:
    """分類近 N 天的相異來源並寫入 ip_intel。回傳統計摘要。"""
    ips = _distinct_ips(days, include_api=include_api)
    now = timewin.fmt(timewin.taipei_now())
    counts: Counter[str] = Counter()
    rows: list[tuple] = []
    for raw in ips:
        fp = masking.src(raw)
        if not fp:
            continue
        c = classify.classify(raw)
        counts[c.source_type] += 1
        # first_seen 與 last_seen 都先給 now；重跑時 ON CONFLICT 只更新 last_seen
        rows.append((fp, c.source_type, c.org, c.country, c.note, now, now, now))

    if not dry_run and rows:
        with db.tx() as conn:
            # 同一個 src 重跑時更新分類但**保留 first_seen** ——
            # 那是「我們第一次見到這個來源」，不該被每日重跑覆蓋掉。
            conn.executemany(
                "INSERT INTO ip_intel"
                " (src, source_type, org, country, note, first_seen, last_seen,"
                "  classified_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(src) DO UPDATE SET"
                "  source_type = excluded.source_type, org = excluded.org,"
                "  country = excluded.country, note = excluded.note,"
                "  last_seen = excluded.last_seen,"
                "  classified_at = excluded.classified_at",
                rows)

    summary = {
        "days": days, "scanned": len(ips), "written": 0 if dry_run else len(rows),
        "by_type": dict(counts.most_common()),
        "suspicious": sum(counts[t] for t in classify.SUSPICIOUS_TYPES),
        "snapshot": ranges.SNAPSHOT, "dry_run": dry_run,
    }
    logger.info("ip_intel 更新：%s", summary)
    return summary


def seed_allowlist(who: str = "intel.refresh") -> int:
    """把分類為 office 的來源播種進 allowlist。

    為什麼需要這一步：`office` 只是「這是什麼」的標記，掃描的抑制讀的是 allowlist
    （見 `sweep/report.allowlisted_fps()`）。實測我方辦公室出口在 94 天內用了
    316 個帳號，不抑制的話它會穩定佔據「憑證集中」第一名、把該查的境外機房 IP
    壓到清單後面。

    只新增、不覆寫既有條目 —— allowlist 是人工核准的東西，程式不該改別人的核准。
    """
    rows = db.rows("SELECT src, org, note FROM ip_intel WHERE source_type = 'office'")
    if not rows:
        return 0
    now = timewin.fmt(timewin.taipei_now())
    added = 0
    with db.tx() as conn:
        for r in rows:
            exists = db.one("SELECT 1 AS x FROM allowlist WHERE source_fp = ?",
                            (r["src"],))
            if exists:
                continue
            conn.execute(
                "INSERT INTO allowlist (name, owner, purpose, integration_type,"
                " source_fp, valid_from, approved_by, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '生效中')",
                (r["org"] or "我方辦公室出口", "Ocard 內部",
                 r["note"] or "內部人員代操客戶後台的共用出口",
                 "內部代操", r["src"], now, who))
            added += 1
    logger.info("allowlist 播種 %d 筆", added)
    return added


def main() -> None:
    setup_logging("intel_refresh.log")
    p = argparse.ArgumentParser(description="建立／更新來源情報 ip_intel")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--include-api", action="store_true",
                   help="一併掃 ods_api_log 的 forwarded header（慢）")
    p.add_argument("--seed-allowlist", action="store_true",
                   help="把 office 型態的來源播種進 allowlist")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    stats = ranges.stats()
    print(f"離線範圍快照 {stats['snapshot']}：{stats['networks']:,} 個網段")
    result = refresh(args.days, include_api=args.include_api, dry_run=args.dry_run)
    wrote = "（dry-run 未寫入）" if args.dry_run else f"寫入 {result['written']:,} 筆"
    print(f"掃描 {result['scanned']:,} 個相異來源，{wrote}")
    for t, n in result["by_type"].items():
        print(f"  {classify.LABELS.get(t, t):<16}{n:>7,}")
    print(f"  其中「真人不會從這裡登入」（機房／VPN）：{result['suspicious']:,}")
    if args.seed_allowlist:
        print(f"allowlist 播種 {seed_allowlist()} 筆")


if __name__ == "__main__":
    main()
