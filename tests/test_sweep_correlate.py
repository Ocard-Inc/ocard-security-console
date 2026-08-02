"""交叉計票與評分的純函式測試（不連 ClickHouse）。

守的是 sweep 最容易靜靜壞掉的一件事：**同一訊號組的多支探針只能算一票**。
壞掉的症狀不是報錯，而是排序失去意義 —— 一個爆量帳號在三支量級類探針同時
命中就拿到三票，分數壓過「量級 + 憑證集中 + 機房來源」這種真正該排前面的組合。
"""
from __future__ import annotations

from console.sweep import correlate, score
from console.sweep.probes import SIGNAL_WEIGHTS, SUFFICIENT_ALONE, probes
from console.sweep.run import Hit


def _hit(probe_id: str, group: str, *, fp: str = "actor_AAA", kind: str = "actor",
         metric: float = 1000, floor: float = 500) -> Hit:
    return Hit(probe_id=probe_id, probe_name=probe_id, probe_summary="",
               signal_group=group, entity_fp=fp, entity_kind=kind,
               metric=metric, floor=floor, evidence={})


# ── 計票 ──────────────────────────────────────────────────────────

def test_same_signal_group_counts_once() -> None:
    """三支同組探針命中 → 只算一個訊號組，仍未達交叉門檻。"""
    hits = [_hit("P01", "volume"), _hit("P03", "volume"), _hit("PXX", "volume")]
    (cand,) = correlate.correlate(hits)
    assert cand.signal_groups == ("volume",)
    strong, weak = correlate.split_by_threshold((cand,))
    assert strong == () and len(weak) == 1


def test_distinct_signal_groups_reach_threshold() -> None:
    hits = [_hit("P01", "volume"), _hit("P02", "concentration")]
    (cand,) = correlate.correlate(hits)
    assert len(cand.signal_groups) == 2
    strong, _ = correlate.split_by_threshold((cand,))
    assert len(strong) == 1


def test_actor_and_src_are_separate_objects() -> None:
    """帳號與來源即使 fingerprint 相同也不合併 —— 它們是不同的對象。"""
    hits = [_hit("P01", "volume", fp="X", kind="actor"),
            _hit("P04", "credential_sharing", fp="X", kind="src")]
    cands = correlate.correlate(hits)
    assert len(cands) == 2
    assert {c.entity_kind for c in cands} == {"actor", "src"}


def test_suppressed_fps_are_dropped() -> None:
    """allowlist 內的來源完全不進候選（例：辦公室出口的大量帳號共用）。"""
    hits = [_hit("P04", "credential_sharing", fp="src_OFFICE", kind="src"),
            _hit("P06", "auth_ratio", fp="src_OFFICE", kind="src")]
    assert correlate.correlate(hits, suppressed_fps={"src_OFFICE"}) == ()


def test_strongest_in_group_uses_ratio_not_raw_metric() -> None:
    """組內代表以 metric/floor 的倍率決定，不是原始 metric ——
    不同探針的 metric 單位不同（帳號數 vs 請求數），直接比大小沒有意義。"""
    small_but_extreme = _hit("P04", "volume", metric=100, floor=10)     # 10 倍
    large_but_ordinary = _hit("P01", "volume", metric=1000, floor=500)  # 2 倍
    (cand,) = correlate.correlate([large_but_ordinary, small_but_extreme])
    assert cand.strongest_in("volume").probe_id == "P04"


# ── 單一訊號豁免 ───────────────────────────────────────────────────

def test_sufficient_alone_group_reaches_threshold_by_itself() -> None:
    """偽造來源標頭沒有無害解釋，單獨命中就該進清單。"""
    hits = [_hit("P09", "source_trust", fp="src_FORGED", kind="src", metric=128, floor=30)]
    strong, weak = correlate.split_by_threshold(correlate.correlate(hits))
    assert len(strong) == 1 and weak == ()
    assert correlate.qualifies_alone(strong[0]) == "source_trust"


def test_sufficient_alone_respects_its_multiple() -> None:
    """credential_sharing 需達地板的 3 倍才豁免；剛好踩到地板的不豁免
    （避免把「一家店兩個店員共用一台電腦」算成事件）。"""
    floor = 10
    just_over = [_hit("P04", "credential_sharing", fp="s1", kind="src",
                      metric=floor * 2, floor=floor)]
    well_over = [_hit("P04", "credential_sharing", fp="s2", kind="src",
                      metric=floor * SUFFICIENT_ALONE["credential_sharing"], floor=floor)]
    assert correlate.qualifies_alone(correlate.correlate(just_over)[0]) is None
    assert correlate.qualifies_alone(correlate.correlate(well_over)[0]) == "credential_sharing"


def test_non_exempt_group_never_qualifies_alone() -> None:
    """非豁免組即使規模極端也要第二個訊號 —— 量大不等於惡意。"""
    hits = [_hit("P01", "volume", metric=10_000_000, floor=500)]
    assert correlate.qualifies_alone(correlate.correlate(hits)[0]) is None


# ── 評分 ──────────────────────────────────────────────────────────

def test_score_sums_one_contribution_per_group() -> None:
    hits = [_hit("P01", "volume"), _hit("P03", "volume"), _hit("P02", "concentration")]
    scored = score.score_candidate(correlate.correlate(hits)[0])
    assert [c.signal_group for c in scored.contributions] == ["volume", "concentration"]


def test_scale_factor_is_clamped_and_monotonic() -> None:
    assert score.scale_factor(500, 500) == 1.0          # 剛好踩到地板
    assert score.scale_factor(100, 500) == 1.0          # 低於地板不給負分
    assert score.scale_factor(5_000, 500) == 2.0        # 10 倍
    assert score.scale_factor(10**9, 500) == 4.0        # 夾在上限
    assert score.scale_factor(5_000, 500) < score.scale_factor(50_000, 500)


def test_rank_orders_by_score_then_breadth() -> None:
    """同分時交叉命中的訊號組越多越前面。

    metric == floor 讓兩者的規模係數都恰好是 1.0，分數才會真的打平：
    wide = new_source(1.0) + off_hours(1.0) = 2.0；narrow = volume(2.0) × 1.0 = 2.0。
    """
    wide = correlate.correlate([
        _hit("A", "new_source", fp="wide", metric=500, floor=500),
        _hit("B", "off_hours", fp="wide", metric=500, floor=500)])[0]
    narrow = correlate.correlate([
        _hit("C", "volume", fp="narrow", metric=500, floor=500)])[0]
    ranked = score.rank([narrow, wide])
    assert ranked[0].score == ranked[1].score == 2.0
    assert ranked[0].candidate.entity_fp == "wide"


# ── 探針表本身的不變量 ─────────────────────────────────────────────

def test_every_probe_group_has_a_weight() -> None:
    """新增 signal_group 卻忘了給權重的話，score.py 會靜靜地當它是 1.0。"""
    for p in probes():
        assert p.signal_group in SIGNAL_WEIGHTS, f"{p.id} 的 {p.signal_group} 缺權重"


def test_probe_ids_unique_and_floors_positive() -> None:
    ids = [p.id for p in probes()]
    assert len(ids) == len(set(ids))
    for p in probes():
        assert p.floor > 0, f"{p.id} 地板必須為正（同時是 severity 的分母）"
        assert p.floor_kind in ("absolute", "per_day"), f"{p.id} floor_kind 非法"


def test_every_probe_sql_is_parameterised_and_readonly() -> None:
    """SQL 必須帶時間範圍與 %(floor)s，且不得出現 now()。

    地板寫成字面值會讓 per_day 的縮放靜靜失效；SQL 內用 now() 會用到
    ClickHouse 的 UTC 時鐘去比台北牆鐘的 create_time。
    """
    for p in probes():
        assert "%(floor)s" in p.sql, f"{p.id} 的門檻未走 %(floor)s"
        assert "%(start)s" in p.sql and "%(end)s" in p.sql, f"{p.id} 缺時間範圍參數"
        assert "now()" not in p.sql.lower(), f"{p.id} 不可在 SQL 內用 now()"
        assert p.sql.lstrip().lower().startswith("select"), f"{p.id} 必須是 SELECT"


def test_nullable_ip_is_always_coalesced() -> None:
    """ods_backend_sys_log.ip 是 Nullable(String)：splitByChar / position 直接吃它
    會拋 ILLEGAL_TYPE_OF_ARGUMENT。這條擋的是「加新探針時忘了 coalesce」。"""
    for p in probes():
        for fn in ("splitByChar(',', ip", "position(ip", "match(ip"):
            assert fn not in p.sql, f"{p.id} 對 Nullable 的 ip 直接呼叫 {fn}"
