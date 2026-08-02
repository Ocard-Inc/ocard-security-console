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
from console.store import db

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


def allowlisted_fps() -> frozenset[str]:
    """生效中 allowlist 的來源 fingerprint。

    掃描層只做「對象層級」的抑制（不看 endpoint），因為掃描的對象就是來源本身。
    規則引擎那邊需要 endpoint 粒度是因為它判定的是「某來源打某端點」。

    實測辦公室出口在 94 天內用了 316 個帳號，不抑制的話它會穩定佔據
    「憑證集中」榜首，把真正需要查的境外機房 IP 壓到後面。
    """
    now = timewin.fmt(timewin.taipei_now())
    rows = db.rows(
        "SELECT source_fp FROM allowlist WHERE status = '生效中'"
        " AND source_fp IS NOT NULL AND source_fp != ''"
        " AND (valid_from IS NULL OR valid_from <= ?)"
        " AND (valid_to IS NULL OR valid_to >= ?)",
        (now, now),
    )
    return frozenset(r["source_fp"] for r in rows)


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


def build(start: datetime, end: datetime, *,
          include_high_cost: bool = False,
          intel_available: bool = False) -> dict:
    """執行一次完整掃描並回傳結構化報告。"""
    probe_run = run.run_probes(start, end, include_high_cost=include_high_cost,
                               intel_available=intel_available)
    suppressed = allowlisted_fps()
    all_fps = {h.entity_fp for h in probe_run.hits}
    candidates = correlate.correlate(probe_run.hits, suppressed_fps=suppressed)
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
                            len(all_fps & suppressed)),
        "findings": [_finding(i, s_, ctx_map.get((s_.candidate.entity_kind,
                                          s_.candidate.entity_fp)))
                     for i, s_ in enumerate(shown, 1)],
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
