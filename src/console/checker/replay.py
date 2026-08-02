"""歷史 replay 回測：以 5 分鐘步進重跑規則引擎，驗證告警觸發。

不寫入 events / known_sources（dry-run），只輸出觸發清單。

CLI：
  uv run python -m console.checker.replay --start "2026-07-16 00:00" --end "2026-07-16 02:00"
  uv run python -m console.checker.replay --start "2026-07-30 21:30" --end "2026-07-30 22:10"
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from unittest.mock import patch

from console.core import timewin
from console.core.logging_setup import setup_logging
from console.rules import engine
from console.rules.loader import load_rules


def replay(start_s: str, end_s: str, step_minutes: int = 5,
           quiet: bool = False) -> list:
    start = timewin.parse(start_s)
    end = timewin.parse(end_s)
    rules = load_rules()
    all_findings = []
    seen: set[str] = set()

    # dry-run：known_sources 查詢照常（比對 seed），但不回寫
    with patch("console.rules.engine.db.tx") as fake_tx:
        fake_tx.return_value.__enter__ = lambda s: _NullConn()
        fake_tx.return_value.__exit__ = lambda s, *a: False
        cursor = start
        while cursor <= end:
            findings, failures = engine.evaluate(rules, cursor)
            for f in findings:
                first = f.entity_key not in seen
                seen.add(f.entity_key)
                all_findings.append((cursor, f, first))
                if not quiet and first:
                    med = f"median {f.baseline_median:.0f}" if f.baseline_median else "無基線"
                    mult = f"（{f.multiple}×）" if f.multiple else ""
                    print(f"[{timewin.fmt(cursor)}] {f.severity} {f.rule.id} "
                          f"{f.rule.name}｜{f.entity_label}｜"
                          f"{f.metric:.0f} ≥ 門檻 {f.threshold:.0f}，{med}{mult}")
            if failures and not quiet:
                print(f"[{timewin.fmt(cursor)}] 規則失敗：{failures}")
            cursor += timedelta(minutes=step_minutes)
    return all_findings


class _NullConn:
    def execute(self, *a, **k):
        return self

    def executemany(self, *a, **k):
        return self


def main() -> None:
    setup_logging("replay.log")
    parser = argparse.ArgumentParser(description="規則 replay 回測（dry-run）")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--summary", action="store_true", help="只輸出統計")
    args = parser.parse_args()
    findings = replay(args.start, args.end, args.step, quiet=args.summary)
    counts = Counter(f[1].rule.id for f in findings)
    uniq = Counter(f[1].rule.id for f in findings if f[2])
    print(f"\n=== 統計（{args.start} ~ {args.end}）===")
    print(f"總命中 {len(findings)} 次；不重複事件 {sum(uniq.values())} 件")
    for rid in sorted(counts):
        print(f"  {rid}: 命中 {counts[rid]} 次 / 不重複 {uniq.get(rid, 0)} 件")


if __name__ == "__main__":
    main()
