"""異常事件 → Log Explorer 的篩選條件推導（`console.api.drilldown`）。

最重要的是 `test_drilldown_actually_returns_rows` —— 它拿**規則自己的 SQL**跑出來的
真實列當成事件，建出 drilldown，再把條件丟回 Explorer 查，斷言查得到資料。
只驗「`validate()` 不拋例外」是不夠的：每個篩選都命中 0 筆也會通過，而那正是
`test_endpoint_suggest.py` 存在要擋的漂移（「選單裡點得到、但點下去查不到東西」）。

第二重要的是 `test_every_entity_shape_is_accounted_for`：將來有人加了一條用
`fp: token` 或新 entity 欄位的規則，會在這裡失敗，而不是靜靜產出一個沒有對象
條件的 drilldown（那會把「這個帳號做了什麼」偷換成「所有人做了什麼」）。
"""
from __future__ import annotations

import math
import re

import pytest

from console.api import drilldown
from console.core.ch import query
from console.queries import explorer
from console.rules.engine import _masked_context
from console.rules.loader import load_rules
from console.rules.model import EntityField, Rule

# 有實際流量的整日區間（同 test_endpoint_suggest）。刻意是**過去**的區間：
# drilldown 會把右界夾到已落地的資料，用「今天」會讓斷言隨時間漂移。
# 長度正好 1440 分鐘 = api 來源 IP 的上限，所以不會被截短。
START, END = "2026-08-01 00:00:00", "2026-08-02 00:00:00"

# 有些規則本來就罕見（R07A 登入失敗暴力嘗試在多數日子只有零星幾筆，
# 而 HAVING 會把它們濾掉）。備援區間讓「規則在測試區間沒有列」不會退化成
# 永久 skip —— 一個永遠跳過的參數化案例等於沒有守住任何東西。
FALLBACK_WINDOWS = [
    ("2026-08-02 00:00:00", "2026-08-03 00:00:00"),
    ("2026-07-16 00:00:00", "2026-07-17 00:00:00"),   # README 記錄的攻擊日
]

# 額外述詞完全可以用 Explorer 的篩選表達的規則 —— 計數必須**相等**。
# 其餘規則只能要求 `>=`，兩種原因：
#
# ① SQL 帶了 drilldown 表達不了的條件：R05 非上班時間、R06
#    `action='login_success'`、R07A/B `login_failed`、R09 `has_error`、
#    R11 cell 類函式。
# ② **對象維度的比對方式不同**：R14 的 entity 是 `route2`（route 的前 2 段、
#    完全相等），而 Explorer 對 backend 的 endpoint 篩選是
#    `startsWith(route, ...)`（見 explorer.FILTER_COLUMN）。前綴比對會多命中
#    同前綴的其他 route（`orderlist/detailed/...` 會被算進 `orderlist/detail`），
#    所以 Explorer 的計數是規則的**上界**而非等值。api 的 endpoint 沒有這個
#    問題（GROUP_BY 與 FILTER_COLUMN 是同一個運算式），所以 R03/R04 仍是 EXACT。
EXACT = {"R01", "R03", "R04", "R08A", "R08B", "R08C", "R10A", "R10B"}

# 已知無法轉成篩選條件的 entity 欄位，以及為什麼。這份清單刻意是白名單：
# 新增規則時若帶了沒想過的欄位，test_every_entity_shape_is_accounted_for 會失敗。
UNMAPPABLE_COLS = {
    "scope": "R09：字面常數 'api_error'，不是欄位值",
    "key": "R12：資料來源名稱，不對應任何單一表的欄位",
}

_LEGACY_FP_RE = re.compile(r"^(?:actor|src|token|resource)_[0-9A-F]{12}$")


def _rules() -> tuple[Rule, ...]:
    return load_rules()


def _rule(rule_id: str) -> Rule:
    return next(r for r in _rules() if r.id == rule_id)


def _fake_event(rule: Rule, context: dict, *, start: str = START, end: str = END) -> dict:
    """把規則 SQL 的一列包成 `_event_public()` 形狀的事件。"""
    return {"evt_no": f"TEST-{rule.id}", "rule_id": rule.id, "source": rule.source,
            "first_seen": start, "last_seen": end, "context": context}


def _top_row(rule: Rule) -> tuple[dict, str, str] | None:
    """規則 SQL 命中最強的一列，以及它是在哪個區間查到的。

    回傳 (row, start, end)；每個備援區間都查不到才回 None。區間要跟著回傳，
    因為 drilldown 的時間範圍就是事件視窗 —— 兩邊用不同的區間比計數毫無意義。
    """
    for start, end in [(START, END), *FALLBACK_WINDOWS]:
        df = query(rule.sql, {"start": start, "end": end})
        if not len(df):
            continue
        top = df.sort_values("metric", ascending=False).iloc[0]
        row = {k: (None if isinstance(v, float) and math.isnan(v) else v)
               for k, v in dict(top).items()}
        return row, start, end
    return None


def _explorer_count(f: dict) -> int:
    """把 drilldown 的 filter 丟回 Explorer，回該區間的總列數。

    刻意驗 `trend` 的總和而不是 `filter["analysis"]` 指定的分析：這個不變量守的是
    **WHERE 子句**選到的母體，與落地時預選哪一種呈現無關。
    """
    ef = explorer.ExplorerFilter(
        source=f["source"], start=f["start"], end=f["end"],
        brand=f.get("brand"), endpoint=f.get("endpoint"),
        source_ip=f.get("source_ip"), actor=f.get("actor"),
        only_error=f.get("only_error", False))
    return sum(b["count"] for b in explorer.trend(ef, "1h")["rows"])


# ───────────────────────── 主不變量 ─────────────────────────

@pytest.mark.parametrize("rule_id", [r.id for r in load_rules() if r.sql],
                         ids=lambda x: x)
def test_drilldown_actually_returns_rows(rule_id):
    """規則命中的那一列，帶到 Explorer 一定查得到 —— 而且母體不小於 metric。"""
    rule = _rule(rule_id)
    found = _top_row(rule)
    if found is None:
        pytest.skip(f"{rule_id} 在所有測試區間內都沒有任何列")
    row, start, end = found

    d = drilldown.build(rule, _fake_event(rule, _masked_context(rule, row),
                                          start=start, end=end))
    assert d["supported"], f"{rule_id} 應該支援跳轉，卻回：{d.get('reason')}"
    assert not d["dropped"], f"{rule_id} 的對象欄位被丟掉了：{d['dropped']}"

    count = _explorer_count(d["filter"])
    metric = float(row["metric"])
    assert count > 0, (
        f"{rule_id} 的 drilldown 條件 {d['filter']} 查不到任何資料 —— "
        "規則 entity 與 explorer 的篩選欄位飄掉了")
    assert count >= metric, (
        f"{rule_id} 的 drilldown 母體（{count}）小於規則 metric（{metric:.0f}），"
        "表示篩選條件比規則本身更嚴格")
    if rule_id in EXACT:
        assert count == pytest.approx(metric), (
            f"{rule_id} 的述詞應該可被 Explorer 完整表達，"
            f"但母體 {count} != metric {metric:.0f}")


def test_every_entity_shape_is_accounted_for():
    """每個 entity 的 (fp, col) 都必須是「有對照」或「明確列為無法對照」。"""
    for rule in _rules():
        for f in rule.entity:
            if f.fp:
                assert f.fp in drilldown._FILTER_BY_FP or f.fp == "token", (
                    f"{rule.id} 的 entity fp={f.fp!r} 沒有對應的 Explorer 篩選欄位，"
                    "也不在已知的不可反查清單（token）裡")
            else:
                assert f.col in drilldown._FILTER_BY_COL or f.col in UNMAPPABLE_COLS, (
                    f"{rule.id} 的 entity col={f.col!r} 沒有對應的 Explorer 篩選欄位。"
                    "請加進 _FILTER_BY_COL，或說明為什麼無法對照並列入 UNMAPPABLE_COLS")


def test_mapped_fields_are_supported_on_their_own_source():
    """對照表指到的篩選欄位，在該規則的資料來源上必須真的可用（否則永遠被丟掉）。"""
    for rule in _rules():
        if rule.source not in ("api", "backend", "admin", "auth"):
            continue
        for f in rule.entity:
            field = (drilldown._FILTER_BY_FP.get(f.fp) if f.fp
                     else drilldown._FILTER_BY_COL.get(f.col))
            if field is None:
                continue
            assert explorer.filter_support(field, rule.source) is None, (
                f"{rule.id}（{rule.source}）的 {f.col} 對應到 {field}，"
                f"但該來源不支援：{explorer.filter_support(field, rule.source)}")


# ───────────────────────── 降級與否決 ─────────────────────────

def test_legacy_fingerprint_is_dropped_not_filtered():
    """改版前的指紋拿去比對 ClickHouse 的原值永遠不相等，必須丟掉而不是送出去。"""
    rule = _rule("R03")
    d = drilldown.build(rule, _fake_event(rule, {
        "src": "src_5D4C6FA090F0", "endpoint": "Api2/GetProfile", "metric": 100}))
    assert d["supported"], "還有一個可用的 endpoint，不該整筆否決"
    assert "source_ip" not in d["filter"]
    assert d["filter"]["endpoint"] == "Api2/GetProfile"
    assert any(x["col"] == "src" for x in d["dropped"])
    for value in d["filter"].values():
        assert not _LEGACY_FP_RE.match(str(value)), f"指紋 {value!r} 流進了篩選條件"


def test_event_with_only_legacy_fingerprints_is_refused():
    """R05 的兩個對象都是指紋時沒有東西可查 —— 要給原因，不是一組空條件。"""
    rule = _rule("R05")
    d = drilldown.build(rule, _fake_event(rule, {
        "acc": "actor_EE061C3CCC88", "ip": "src_6F9060BF7A16", "metric": 30}))
    assert d["supported"] is False
    assert "指紋" in d["reason"]


def test_scrubbed_value_is_dropped():
    """`_masked_context` 清洗過或截斷過的值當前綴會靜靜命中 0 筆。"""
    rule = _rule("R04")
    d = drilldown.build(rule, _fake_event(rule, {
        "endpoint": "Api2/Get***", "metric": 100}))
    assert d["supported"] is False


def test_missing_entity_column_is_dropped():
    """`_masked_context` 跳過 None，所以 entity 欄位可能整個不存在。"""
    rule = _rule("R10A")
    d = drilldown.build(rule, _fake_event(rule, {"metric": 100}))
    assert d["supported"] is False
    assert any(x["col"] == "_brand" for x in d["dropped"])


def test_brand_float_becomes_int():
    """context 的 `_brand` 是 float（pandas 把純數值列升成 float64）。"""
    rule = _rule("R10B")
    d = drilldown.build(rule, _fake_event(rule, {"_brand": 4748.0, "metric": 100}))
    assert d["filter"]["brand"] == 4748
    assert isinstance(d["filter"]["brand"], int)


def test_freshness_rule_is_refused_with_a_reason():
    """R12 的來源是 `all`，沒有對應的單一資料表。"""
    rule = _rule("R12")
    d = drilldown.build(rule, {"evt_no": "TEST-R12", "rule_id": "R12", "source": "all",
                               "first_seen": START, "last_seen": END,
                               "context": {"table": "ods_api_log"}})
    assert d["supported"] is False
    assert d["reason"]


def test_orphan_event_is_refused_with_a_reason():
    """規則被改名或移除後，既有事件仍在（events 不是衍生表）。"""
    d = drilldown.build(None, {"evt_no": "TEST-X", "rule_id": "R99", "source": "api",
                               "first_seen": START, "last_seen": END, "context": {}})
    assert d["supported"] is False
    assert "R99" in d["reason"]


def test_build_never_raises():
    """`build()` 由事件詳細頁呼叫，任何例外都會 500 掉整頁。"""
    rule = _rule("R03")
    for bad in ({}, {"source": "api"}, {"source": "api", "context": None},
                {"source": "api", "first_seen": "壞掉的時間", "last_seen": END,
                 "context": {"src": "1.2.3.4"}}):
        out = drilldown.build(rule, bad)
        assert "supported" in out


# ───────────────────────── 分析方式與時間區間 ─────────────────────────

def test_only_error_never_combines_with_error_analysis():
    """`error_analysis` 的 total 與 errors 共用 WHERE，併用會讓錯誤率全變 100%。"""
    for rule in _rules():
        found = _top_row(rule) if rule.sql else None
        if found is None:
            continue
        row, start, end = found
        d = drilldown.build(rule, _fake_event(rule, _masked_context(rule, row),
                                             start=start, end=end))
        if not d["supported"]:
            continue
        f = d["filter"]
        assert not (f["only_error"] and f["analysis"] == "error"), rule.id


def test_object_events_land_on_trend():
    """帶了帳號或來源 IP 時落在趨勢 —— 那是事件頁結構上給不出的東西
    （事件頁的圖是整個資料來源的量，不是該對象的）。"""
    rule = _rule("R01")
    d = drilldown.build(rule, _fake_event(rule, {
        "acc": "andrew_c", "ip": "1.2.3.4", "metric": 100}))
    assert d["filter"]["analysis"] == "trend"


def test_error_scope_rule_lands_on_error_analysis():
    rule = _rule("R09")
    d = drilldown.build(rule, _fake_event(rule, {"scope": "api_error", "metric": 100}))
    assert d["supported"], d.get("reason")
    assert d["filter"]["analysis"] == "error"
    assert d["filter"]["only_error"] is False


def test_admin_login_rule_lands_on_endpoint_ranking():
    """R06 的前綴篩會連 login_failed 一起撈到；endpoint 排名會把兩者分成兩列。"""
    rule = _rule("R06")
    d = drilldown.build(rule, _fake_event(rule, {
        "endpoint": "Boss_initial/auth_v2", "metric": 200}))
    assert d["filter"]["analysis"] == "endpoint"


def test_window_end_never_exceeds_landed_data():
    rule = _rule("R08C")
    d = drilldown.build(rule, _fake_event(
        rule, {"src": "1.2.3.4", "metric": 100},
        start="2026-08-01 00:00:00", end="2099-01-01 00:00:00"))
    assert d["filter"]["end"] < "2099-01-01 00:00:00"


def test_long_window_with_api_source_ip_is_clamped_and_reported():
    """api 的來源 IP 要解析 headers JSON，長區間會跑上數十秒。"""
    rule = _rule("R08C")
    d = drilldown.build(rule, _fake_event(
        rule, {"src": "1.2.3.4", "metric": 100},
        start="2026-06-01 00:00:00", end="2026-07-01 00:00:00"))
    w = d["window"]
    assert w["clamped"] is True
    assert w["max_minutes"] == 24 * 60
    # 截短這件事必須說出來，畫面才有東西可顯示 —— 靜靜截斷是本專案的失敗類型
    assert w["full_start"] == "2026-06-01 00:00:00"
    assert w["full_end"] == "2026-07-01 00:00:00"
    assert d["filter"]["start"] == "2026-06-30 00:00:00"


def test_window_is_not_clamped_when_no_json_ip_filter():
    """同樣一個月，backend 不必解析 headers，不該被截到 24 小時。"""
    rule = _rule("R14")
    d = drilldown.build(rule, _fake_event(
        rule, {"route2": "orderlist/detail", "metric": 500},
        start="2026-06-01 00:00:00", end="2026-07-01 00:00:00"))
    assert d["window"]["clamped"] is False


# ───────────────────────── API 接線 ─────────────────────────

def test_event_detail_carries_drilldown(client):
    rows = client.get("/api/events?hours=2160").json()["events"]
    if not rows:
        pytest.skip("state/monitor.db 沒有事件")
    d = client.get(f"/api/events/{rows[0]['evt_no']}").json()["drilldown"]
    assert isinstance(d, dict) and "supported" in d
    if d["supported"]:
        assert d["filter"]["start"] and d["filter"]["end"]
        for value in d["filter"].values():
            assert not _LEGACY_FP_RE.match(str(value))


def test_explorer_accepts_every_stored_events_drilldown(client):
    """SQLite 裡每一筆事件的 drilldown 都要能被 Explorer 接受（不是 400／502）。"""
    for e in client.get("/api/events?hours=2160").json()["events"]:
        d = client.get(f"/api/events/{e['evt_no']}").json()["drilldown"]
        if not d["supported"]:
            continue
        r = client.post("/api/explorer", json={**d["filter"], "limit": 20})
        assert r.status_code == 200, f"{e['evt_no']}：{r.status_code} {r.text[:200]}"


def test_explorer_rejects_non_integer_brand(client):
    """品牌解不出整數時要明確 400，不可靜靜丟掉條件回一份全品牌結果。"""
    r = client.post("/api/explorer", json={
        "source": "api", "start": START, "end": END, "brand": "瓦城", "analysis": "trend"})
    assert r.status_code == 400


def test_unknown_entity_field_has_no_silent_mapping():
    """`filter_support` 是「哪個篩選在哪張表可用」的唯一真相。"""
    assert explorer.filter_support("nope", "api")
    assert explorer.filter_support("actor", "auth")
    assert explorer.filter_support("endpoint", "auth")
    assert explorer.filter_support("actor", "backend") is None
    assert explorer.filter_support("brand", "auth") is None


def test_token_entity_is_refused():
    """假想一條以 API token 為對象的規則：指紋不可反查，必須否決而不是送指紋。"""
    rule = Rule(id="RXX", name="測試", severity="P2", source="auth", kind="sql_threshold",
                window_minutes=10, enabled=True, sql="SELECT 1",
                entity=(EntityField(col="token", fp="token"),))
    d = drilldown.build(rule, _fake_event(rule, {"token": "token_ABCDEF012345", "metric": 1}))
    assert d["supported"] is False
