"""守「基線必須取自區間之外」與「地板隨區間長度縮放」這兩件事。

會實際連 ClickHouse（比照其他測試）。守的是同一類錯誤：**不會報錯，只會給錯數字**。

情境一：分析師拉「只含攻擊那兩天」的區間查 andrew_c。如果基線用區間內算，
median 就是攻擊本身的量（40 萬），倍數變 1.x，最重大的事件靜靜消失。
正確行為是基線落在 [start-28d, start)，倍數維持數萬倍。

情境二：同一起事件在 3 天與 94 天的區間應該得到**相同的風險分數**。
分數若隨區間長度漂移，「拉長區間」就變成「悄悄改變判定」。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.core.masking import actor
from console.sweep import correlate, run, score
from console.sweep.probes import ABSOLUTE, PER_DAY, Probe, probes

# 報告事件 1：2026-07-16~17 兩日內 120 萬次 orderlist/detail。
ATTACK_START = timewin.parse("2026-07-16 00:00:00")
ATTACK_END = timewin.parse("2026-07-18 00:00:00")
# 對象現在是原始帳號，不是指紋（見 core/masking.py）。仍走 masking.actor()
# 而不是寫死字串 —— 那是探針產生對象值的同一條路徑。
ATTACKER = actor("andrew_c")


@pytest.fixture(scope="module")
def attack_window_hits() -> tuple:
    """只含攻擊兩天的區間 —— 最容易讓基線被污染的情境。"""
    return run.run_probes(ATTACK_START, ATTACK_END).hits


def test_baseline_comes_from_before_the_range(attack_window_hits) -> None:
    """P01 的 median_prev 必須是攻擊**之前**的日常水準，不是攻擊期間的量。"""
    p01 = [h for h in attack_window_hits
           if h.probe_id == "P01" and h.entity_fp == ATTACKER]
    assert p01, "andrew_c 未被 P01 命中 —— 基線或地板的設定壞了"
    hit = p01[0]
    median_prev = hit.evidence["median_prev"]

    assert median_prev is not None, "區間之前應有 andrew_c 的歷史"
    # 攻擊前該帳號日均不足 100 次；區間內單日峰值 77 萬。
    assert median_prev < 500, (
        f"median_prev={median_prev} 太高，基線很可能被區間內的攻擊量污染 —— "
        "檢查 run.build_params() 的 prev_start 是否由 start（而非 end）往回推")
    assert hit.metric > 100_000, "區間內的單日峰值應為攻擊當日的量"
    assert hit.metric / median_prev > 1000, (
        "峰值相對自身基線的倍數應為三個數量級以上（報告判讀為 5.5 萬倍）")


def test_params_derive_baseline_from_start_not_end() -> None:
    """參數層面直接檢查：prev_start / seed_start 一律由 start 往回推。"""
    start, end = timewin.parse("2026-07-16"), timewin.parse("2026-07-18")
    p = run.build_params(start, end)
    assert p["start"] == timewin.fmt(start) and p["end"] == timewin.fmt(end)
    assert timewin.parse(p["prev_start"]) < start
    assert timewin.parse(p["seed_start"]) <= timewin.parse(p["prev_start"])


def test_attacker_ranks_first_in_a_window_containing_only_the_attack(
        attack_window_hits) -> None:
    strong, _ = correlate.split_by_threshold(correlate.correlate(attack_window_hits))
    ranked = score.rank(strong)
    assert ranked, "攻擊區間應至少有一個達門檻的對象"
    assert ranked[0].candidate.entity_fp == ATTACKER
    assert ranked[0].level == "極高"


def test_score_is_independent_of_range_length(attack_window_hits) -> None:
    """同一起事件在 2 天與 94 天的區間應得到相同分數。

    這是把 P01/P02/P03/P10 的 metric 全部改成「單日峰值」的理由：用區間總量的話
    andrew_c 在 3 天區間是 18.0 分、在 93 天區間掉到 14.55 分 —— 嚴重程度變成
    「分析師選了多長的區間」的函數。
    """
    def score_of(hits) -> float:
        strong, _ = correlate.split_by_threshold(correlate.correlate(hits))
        for s in score.rank(strong):
            if s.candidate.entity_fp == ATTACKER:
                return s.score
        pytest.fail("andrew_c 未進入事件清單")

    narrow = score_of(attack_window_hits)
    wide = score_of(run.run_probes(ATTACK_START - timedelta(days=60),
                                   ATTACK_END + timedelta(days=14)).hits)
    assert narrow == wide, (
        f"窄區間 {narrow} 分、寬區間 {wide} 分 —— 有探針的 metric 是區間總量而非"
        "單日峰值，嚴重程度會隨區間長度漂移")


# ── 地板縮放 ──────────────────────────────────────────────────────

def _probe(kind: str) -> Probe:
    return Probe(id="T", name="t", summary="", source="backend", signal_group="volume",
                 entity_kind="actor", fp_kind="actor", floor=100, floor_kind=kind, sql="")


def test_per_day_floor_scales_with_range() -> None:
    assert run.effective_floor(_probe(PER_DAY), 1) == 100
    assert run.effective_floor(_probe(PER_DAY), 30) == 3000


def test_absolute_floor_ignores_range() -> None:
    assert run.effective_floor(_probe(ABSOLUTE), 1) == 100
    assert run.effective_floor(_probe(ABSOLUTE), 90) == 100


def test_short_range_does_not_collapse_per_day_floor() -> None:
    """不足一天的區間不可把 per_day 地板算成接近 0（那會讓所有東西都命中）。"""
    start = timewin.parse("2026-07-16 00:00:00")
    assert run.range_days(start, start + timedelta(minutes=10)) == 1.0
    assert run.effective_floor(_probe(PER_DAY), run.range_days(
        start, start + timedelta(minutes=10))) == 100


def test_peak_day_probes_are_absolute() -> None:
    """metric 是單日峰值的探針必須是 absolute —— 對峰值再乘天數毫無意義。

    判斷依據：SQL 以 max(...) 產生 metric。
    """
    for p in probes():
        first_metric_line = next(
            (ln for ln in p.sql.splitlines() if "AS metric" in ln), "")
        if "max(" in first_metric_line:
            assert p.floor_kind == ABSOLUTE, (
                f"{p.id} 的 metric 是單日峰值卻用 per_day 地板")
