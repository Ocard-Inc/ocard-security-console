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
from console.rules.effective import effective_rules, yaml_values
from console.rules.loader import load_rules
from console.store import rule_overrides


def override_banner() -> str:
    """回測用的是 YAML 值還是覆寫後的生效值 —— 必須印出來。

    兩者不同時，回測結論與線上行為不一致，而且**沒有任何提示**。
    「我回測過了，這條規則不會誤報」在覆寫存在時可能是錯的。
    """
    overrides = rule_overrides.all_overrides()
    if not overrides:
        return "規則參數：全部沿用 YAML（無覆寫）"
    lines = ["規則參數：使用生效值（YAML + 覆寫）"]
    rules = {r.id: r for r in load_rules()}
    for rid in sorted(overrides):
        rule = rules.get(rid)
        if rule is None:
            continue
        base = yaml_values(rule)
        diff = [f"{f} {base.get(f)}→{v}" for f, v in overrides[rid].items()
                if not f.startswith("_") and f in base and v != base[f]]
        if diff:
            lines.append(f"  {rid}: {'、'.join(diff)}"
                         f"（{overrides[rid].get('_updated_by') or '未知'}）")
    return "\n".join(lines)


def replay(start_s: str, end_s: str, step_minutes: int = 5,
           quiet: bool = False) -> list:
    start = timewin.parse(start_s)
    end = timewin.parse(end_s)
    rules = effective_rules()
    all_findings = []
    all_suppressed = []
    seen: set[str] = set()

    # dry-run：known_sources 查詢照常（比對 seed），但不回寫
    with patch("console.rules.engine.db.tx") as fake_tx:
        fake_tx.return_value.__enter__ = lambda s: _NullConn()
        fake_tx.return_value.__exit__ = lambda s, *a: False
        cursor = start
        while cursor <= end:
            findings, failures, suppressed = engine.evaluate(rules, cursor)
            all_suppressed.extend(suppressed)
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
    # replay 是 dry-run：**不寫 rule_suppressions**（同不寫 events / known_sources）。
    # 抑制只出現在統計裡 —— 這讓「這條例外會遮掉多少東西」在建立之前就能回測。
    replay.last_suppressed = all_suppressed
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
    print(override_banner())
    findings = replay(args.start, args.end, args.step, quiet=args.summary)
    counts = Counter(f[1].rule.id for f in findings)
    uniq = Counter(f[1].rule.id for f in findings if f[2])
    print(f"\n=== 統計（{args.start} ~ {args.end}）===")
    print(f"總命中 {len(findings)} 次；不重複事件 {sum(uniq.values())} 件")
    for rid in sorted(counts):
        print(f"  {rid}: 命中 {counts[rid]} 次 / 不重複 {uniq.get(rid, 0)} 件")

    suppressed = getattr(replay, "last_suppressed", [])
    if suppressed:
        by_entry = Counter((s.allowlist_id, s.allowlist_name, s.source_ip)
                           for s in suppressed)
        print(f"\n=== Allowlist 抑制（{len(suppressed)} 次）===")
        for (eid, name, ip), n in by_entry.most_common():
            rules_hit = sorted({s.rule_id for s in suppressed if s.allowlist_id == eid})
            print(f"  #{eid} {name}（{ip}）：{n} 次，涉及 {'、'.join(rules_hit)}")
    else:
        print("\nAllowlist 抑制：0 次")


if __name__ == "__main__":
    main()
