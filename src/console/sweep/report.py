"""把掃描結果組裝成結構化報告。

輸出是一個純 dict（可 JSON 序列化、可落盤、可餵給 narrate.py），**不含任何原始
識別值** —— 對象一律是 fingerprint，數值一律已正規化。

流程：
    run.run_probes → correlate.correlate → correlate.split_by_threshold
    → score.rank → limits.collect → build()
"""
from __future__ import annotations

import logging
from datetime import datetime

from console.core import timewin
from console.sweep import context, correlate, describe, limits, run, score
from console.sweep.probes import SIGNAL_WEIGHTS, by_id
from console.store import allowlist

logger = logging.getLogger(__name__)

# 事件清單的呈現上限。超過的部分不是丟掉，而是在 summary 註明還有幾個 ——
# 靜靜截斷會讓讀者以為清單就是全部。
MAX_FINDINGS = 50

# signal_group → 給人看的名稱。與 probes.SIGNAL_WEIGHTS 的鍵一一對應。
GROUP_LABELS = {
    "source_type": "來源型態（機房／VPN）",
    "source_trust": "來源位址不可信",
    "credential_sharing": "憑證集中",
    "volume": "量級突變",
    "brute_force": "登入失敗集中",
    "concentration": "路由集中",
    "auth_ratio": "認證比例失衡",
    "new_source": "首見來源",
    "off_hours": "非上班時間",
}


# allowlist 的讀取一律走 store/allowlist.py（那是這張表的唯一入口）。
# 掃描用 global_source_ips()：**只有 rule_id IS NULL 的全域條目**。
# 規則範圍的例外不影響掃描 —— 掃描不跑規則，套上去沒有意義；
# 而漏掉那個條件的話，一筆「只對 R07B」的條目會讓該來源從整份報告消失。
#
# 掃描層也刻意不看 endpoint（規則引擎才需要，因為它判定的是「某來源打某端點」）。
#
# 實測辦公室出口在 94 天內用了 316 個帳號，不抑制的話它會穩定佔據
# 「憑證集中」榜首，把真正需要查的境外機房 IP 壓到後面。


def _finding(rank: int, scored: score.Scored, ctx: dict | None) -> dict:
    c = scored.candidate
    alone = correlate.qualifies_alone(c)
    single = len(c.signal_groups) < correlate.MIN_SIGNAL_GROUPS
    return {
        "rank": rank,
        # 原始帳號／IP。欄位名刻意不叫 entity_fp —— 它已經不是指紋了
        #（見 core/masking.py 的模組說明）。
        "entity": c.entity_fp,
        "entity_kind": c.entity_kind,
        "entity_kind_label": "帳號" if c.entity_kind == "actor" else "來源",
        # 清單上直接讀得懂的一句話：誰、哪些品牌、什麼時間、發生什麼
        "headline": describe.headline(c.hits, ctx),
        "explains": describe.explains(c.hits),
        "context": ctx or {},
        "risk_level": scored.level,
        "score": scored.score,
        "signal_groups": [
            {"key": g, "label": GROUP_LABELS.get(g, g), "weight": SIGNAL_WEIGHTS.get(g, 1.0)}
            for g in c.signal_groups
        ],
        # 單一訊號豁免要標出來：讀者必須知道這一列沒有交叉驗證
        "single_signal": single,
        "single_signal_reason": (
            f"「{GROUP_LABELS.get(alone, alone)}」單獨成立即足以列入 —— 此類訊號沒有無害的解釋"
            if single and alone else None),
        "contributions": [
            {"signal_group": g.signal_group, "label": GROUP_LABELS.get(g.signal_group,
                                                                      g.signal_group),
             "probe_id": g.probe_id, "probe_name": g.probe_name,
             "weight": g.weight, "scale": g.scale, "points": g.points}
            for g in scored.contributions
        ],
        "hits": [
            {"probe_id": h.probe_id, "probe_name": h.probe_name,
             "probe_summary": h.probe_summary,
             "signal_group": h.signal_group,
             "metric": h.metric, "floor": h.floor,
             "multiple_of_floor": round(h.metric / max(h.floor, 1e-9), 1),
             "explain": describe.phrase(h),
             "evidence": h.evidence}
            for h in sorted(c.hits, key=lambda x: x.probe_id)
        ],
    }


def _summary(start: datetime, end: datetime, probe_run: run.ProbeRun,
             ranked: tuple[score.Scored, ...], weak_count: int,
             suppressed: int) -> dict:
    by_level: dict[str, int] = {}
    for s in ranked:
        by_level[s.level] = by_level.get(s.level, 0) + 1
    executed = sorted(set(probe_run.timings_ms) - set(probe_run.failures))
    return {
        "range_start": timewin.fmt(start),
        "range_end": timewin.fmt(end),
        "range_days": round((end - start).total_seconds() / 86400, 2),
        "total_hits": len(probe_run.hits),
        "findings": len(ranked),
        "findings_shown": min(len(ranked), MAX_FINDINGS),
        "findings_truncated": max(0, len(ranked) - MAX_FINDINGS),
        "by_level": by_level,
        "single_signal_dropped": weak_count,
        "allowlist_suppressed": suppressed,
        "probes_executed": executed,
        "probes_skipped": list(probe_run.skipped),
        "probes_failed": probe_run.failures,
        "probe_ms": probe_run.timings_ms,
        "min_signal_groups": correlate.MIN_SIGNAL_GROUPS,
    }


def _suppressed_detail(probe_run: run.ProbeRun,
                       suppressed_srcs: frozenset[str]) -> list[dict]:
    """被 allowlist 擋掉的來源，含「若不抑制會是第幾名」。

    抑制原本只留下一個計數，於是唯一看得出這個盲區的地方是一個數字 ——
    而「這個 IP 若不在 Allowlist 內會是本次掃描第 1 名（極高）」才是判斷
    一條例外還該不該存在的句子。

    做法與 `split_by_threshold` 刻意保留 weak 候選同一個理由：靜靜濾掉會讓
    讀者以為清單就是全部。成本可忽略（純 Python，對象數是個位數）。
    """
    hidden = [h for h in probe_run.hits if correlate.is_suppressed(h, suppressed_srcs)]
    if not hidden:
        return []
    entries = {e.source_ip: e for e in allowlist.active_entries()}
    # 被抑制的對象自己走一次完整的評分流程，才知道它「本來」會在哪個位置。
    # 名次要放回**完整**清單裡比（未抑制的 + 被抑制的），否則「第 1 名」的
    # 意思會變成「被抑制的那幾個裡面的第 1 名」，那是另一件事。
    hidden_ranked = score.rank(
        correlate.split_by_threshold(correlate.correlate(hidden))[0])
    visible = score.rank(correlate.split_by_threshold(
        correlate.correlate(probe_run.hits, suppressed_srcs=suppressed_srcs))[0])
    combined = sorted([*visible, *hidden_ranked],
                      key=lambda s: (s.score, len(s.contributions)), reverse=True)
    rank_of = {(s.candidate.entity_kind, s.candidate.entity_fp): i
               for i, s in enumerate(combined, 1)}

    out = []
    for s_ in hidden_ranked:
        c = s_.candidate
        entry = entries.get(c.entity_fp)
        out.append({
            "entity": c.entity_fp,
            "entity_kind": c.entity_kind,
            "allowlist": None if entry is None else {
                "id": entry.id, "name": entry.name, "valid_to": entry.valid_to},
            "signal_groups": [{"key": g, "label": GROUP_LABELS.get(g, g)}
                              for g in c.signal_groups],
            "hits": [{"probe_id": h.probe_id, "probe_name": h.probe_name,
                      "metric": h.metric, "floor": h.floor,
                      "multiple_of_floor": round(h.metric / max(h.floor, 1e-9), 2)}
                     for h in sorted(c.hits, key=lambda x: x.probe_id)],
            "would_be_level": s_.level,
            "would_be_score": s_.score,
            "would_be_rank": rank_of.get((c.entity_kind, c.entity_fp)),
        })
    return out


def build(start: datetime, end: datetime, *,
          include_high_cost: bool = False,
          intel_available: bool = False) -> dict:
    """執行一次完整掃描並回傳結構化報告。"""
    probe_run = run.run_probes(start, end, include_high_cost=include_high_cost,
                               intel_available=intel_available)
    suppressed = allowlist.global_source_ips()
    # 計數要 kind-aware，否則一個字面上等於某帳號名的條目會把數字灌水
    hidden = [h for h in probe_run.hits if correlate.is_suppressed(h, suppressed)]
    suppressed_count = len({h.entity_fp for h in hidden})
    candidates = correlate.correlate(probe_run.hits, suppressed_srcs=suppressed)
    strong, weak = correlate.split_by_threshold(candidates)
    ranked = score.rank(strong)

    # 上下文只查會呈現的那幾筆，不查全部候選
    shown = ranked[:MAX_FINDINGS]
    by_kind: dict[str, list[str]] = {}
    for s_ in shown:
        by_kind.setdefault(s_.candidate.entity_kind, []).append(s_.candidate.entity_fp)
    ctx_map = context.collect(by_kind, start, end)

    return {
        "summary": _summary(start, end, probe_run, ranked, len(weak),
                            suppressed_count),
        "findings": [_finding(i, s_, ctx_map.get((s_.candidate.entity_kind,
                                          s_.candidate.entity_fp)))
                     for i, s_ in enumerate(shown, 1)],
        # 被 allowlist 擋掉的對象。跟著 summary_json 一起落盤，所以重看舊掃描
        # 時看到的是**當時**的抑制狀況 —— allowlist 事後改了也不會改寫歷史
        # （同 limitations「產出當下的事實，不可事後重算」）。
        "suppressed": _suppressed_detail(probe_run, suppressed),
        "limitations": [
            {"key": l.key, "title": l.title, "detail": l.detail, "level": l.level}
            for l in limits.collect(start, end, probe_run)
        ],
        "probes": [
            {"id": p.id, "name": p.name, "summary": p.summary,
             "signal_group": p.signal_group, "cost": p.cost,
             "executed": p.id in probe_run.timings_ms and p.id not in probe_run.failures}
            for p in (by_id(pid) for pid in
                      [*probe_run.timings_ms, *probe_run.skipped]) if p
        ],
        "generated_at": timewin.fmt(timewin.taipei_now()),
    }
