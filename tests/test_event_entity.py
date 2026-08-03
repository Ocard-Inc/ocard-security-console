"""對象視角面板（`queries/entity.py` / `entity_history.py` 與兩個端點）。

這個檔案守的是**單位**與**降級**兩件事，因為兩者壞掉時都不會報錯：

- 單位：母體排名若與事件指標不同單位，畫面會顯示一個精確的錯數字。
  這正是催生這次改版的 bug —— R03 的 metric 是 per (src, endpoint)，
  而它的基線是 per src，實測 P99 差 26 倍。
- 降級：對象不可追蹤時必須明說，**不可以退回畫全站圖假裝有內容**。
"""
from __future__ import annotations

import pytest

from console.queries import entity, entity_history, explorer, exprs


# --- 純函式：不需要 ClickHouse ------------------------------------------------

def test_from_filters_without_usable_dimension_returns_none():
    """R09（entity 是字面常數 scope）與 R12（沒有 entity）走這條路。

    回 None 不是缺陷，是「這條規則沒有可追蹤的對象」。呼叫端據此明說不適用，
    所以這裡回 None 比回一個空的 EntityRef 重要 —— 空的 ref 會產生
    `WHERE`（空字串）而把整張表當成「這個對象」。
    """
    assert entity.from_filters("api", {}) is None
    assert entity.from_filters("api", {"source_ip": None}) is None
    assert entity.from_filters("不存在的來源", {"source_ip": "1.2.3.4"}) is None


def test_entity_expr_is_the_group_by_expression_not_the_prefix_column():
    """對象比對必須用 `GROUP_BY` 的運算式（完全相等），不是 Explorer 的前綴欄位。

    api 的 `FILTER_COLUMN` 剛好也是 ENDPOINT，但 backend 兩者刻意不同：
    篩選作用在完整 `route`（前綴比對），而規則 entity 的 `route2` 是前 2 段。
    用 `FILTER_COLUMN` 去比 route2 的值，`orderlist/detail` 會連
    `orderlist/detail/12345` 一起算進來 —— 數字比事件大而且不會報錯。
    """
    assert explorer.entity_expr("endpoint", "backend") == exprs.ROUTE2
    assert explorer.entity_expr("endpoint", "backend") != explorer.FILTER_COLUMN["backend"]
    assert explorer.entity_expr("endpoint", "api") == exprs.ENDPOINT
    assert explorer.entity_expr("source_ip", "api") == exprs.API_SRC_IP
    # 未知的欄位要回 None，不可以拋例外（呼叫端靠 None 做逐欄位降級）
    assert explorer.entity_expr("不存在的欄位", "api") is None
    assert explorer.entity_expr("endpoint", "不存在的來源") is None


def test_entity_expr_alone_is_not_the_permission_check():
    """`entity_meta()` 有運算式 ≠ 這個組合可以拿來篩選。

    `GROUP_BY["endpoint"]["auth"]` 是 `action`（ods_auth_log 沒有 function 欄位），
    所以 `entity_expr("endpoint", "auth")` 會回一個運算式 —— 但 Explorer 不支援
    對 auth 做 endpoint 篩選。守門的是 `filter_support()`，而 `from_filters()`
    吃的是 `drilldown.build()` **已經過那道檢查**的結果。

    這個測試存在是為了讓「有人改成直接從規則 entity 推 EntityRef、跳過
    drilldown」這件事會有測試失敗 —— 那條捷徑會讓不支援的組合靜靜產生一個
    永遠命中 0 筆的面板。
    """
    assert explorer.entity_expr("endpoint", "auth") is not None
    assert explorer.filter_support("endpoint", "auth") is not None, \
        "auth 不支援 endpoint 篩選，這是 from_filters 依賴的上游守門"


def test_entity_meta_carries_the_masking_kind():
    """遮罩種類必須跟運算式一起拿。分兩次拿的話，遲早出現「該遮的沒遮」。"""
    expr, mask, label = explorer.entity_meta("source_ip", "api")
    assert expr == exprs.API_SRC_IP
    assert mask == "src", "來源 IP 的呈現種類必須是 src（政策：原樣顯示，但走同一個入口）"
    assert label
    # endpoint 不是識別值，不該有遮罩種類
    assert explorer.entity_meta("endpoint", "api")[1] is None


def test_flatness_refuses_to_invent_a_ratio_when_an_hour_is_empty():
    """有任何一小時是 0 時不給比值 —— 那會是無限大。

    把 0 當 1 來算會生出一個看起來精確的假數字，而「只有 N 小時有活動」
    本身就是「像人而不像常駐程式」的證據，比一個假比值有用。
    """
    machine = entity._flatness([100] * 24)
    assert machine["active_hours"] == 24
    assert machine["ratio"] == 1.0
    assert machine["note"] is None

    human = entity._flatness([0] * 8 + [100] * 10 + [0] * 6)
    assert human["active_hours"] == 10
    assert human["ratio"] is None, "有空的小時就不可以給比值"
    assert "沒有活動" in human["note"]

    silent = entity._flatness([0] * 24)
    assert silent["active_hours"] == 0
    assert silent["ratio"] is None


def test_p95_survives_single_sample():
    """`statistics.quantiles` 在 n < 2 時會拋 StatisticsError，
    而「事件之前只有一個分桶」在剛觸發的對象上是真的會發生的。"""
    assert entity_history._p95([]) == 0.0
    assert entity_history._p95([7]) == 7.0
    assert entity_history._p95([1, 2, 3, 4, 5]) >= 4


# --- 端點：會實際連 ClickHouse -------------------------------------------------

def _events(client, limit: int = 8) -> list[dict]:
    body = client.get("/api/events").json()
    return body["events"][:limit]


# 對象面板要打 ClickHouse（實測每個事件約 3 秒），逐測試重跑會讓這個檔案變成
# 整套測試裡最慢的一個。但 fixture **不能用 module 範圍** —— conftest 的
# `_offline_auth` 是 function 範圍的 autouse，module 範圍的 fixture 會排在它
# 之前執行，於是請求還帶著真實的 ROS 設定、回 401，而症狀是
# `body["events"]` 的 KeyError（看起來像端點壞了，其實是認證還沒關掉）。
_PAYLOAD_CACHE: list[tuple[dict, dict]] = []


@pytest.fixture
def entity_payloads(client) -> list[tuple[dict, dict]]:
    """(事件, 對象面板回應) 的清單，整個檔案只實際查一次。"""
    if not _PAYLOAD_CACHE:
        for e in _events(client):
            r = client.get(f"/api/events/{e['evt_no']}/entity")
            assert r.status_code == 200, f"{e['evt_no']} → {r.status_code} {r.text[:200]}"
            _PAYLOAD_CACHE.append((e, r.json()))
    assert _PAYLOAD_CACHE, "DB 裡沒有事件，這個檔案會整個空跑"
    return _PAYLOAD_CACHE


def test_entity_panels_answer_the_three_questions(entity_payloads):
    """有對象的事件必須同時給出母體位置與作息 —— 那是「跟其他人差多少」
    與「這是機器還是人」的答案。少了任何一個，這一頁又退回只能看全站流量。"""
    supported = [(e, p) for e, p in entity_payloads if p["supported"]]
    assert supported, "沒有任何一個事件產生對象面板，改版等於沒生效"
    for e, p in supported:
        where = e["evt_no"]
        assert p["label"], f"{where} 少了對象標籤"
        assert p["dims"], f"{where} 少了維度說明"

        peers = p["peers"]
        assert peers["groups"] >= 1, f"{where} 母體規模為 0"
        assert 1 <= peers["rank"] <= peers["groups"], f"{where} 排名超出母體範圍"
        assert peers["median"] <= peers["p95"] <= peers["p99"] <= peers["max"], \
            f"{where} 分位數不是遞增的"
        # 母體規模必須說出來：只給「第 2 名」而不說「共 6,067 個」的話，
        # 看的人無法判斷那是嚴重還是普通。
        assert "groups" in peers

        prof = p["profile"]
        assert len(prof["rows"]) == 24, f"{where} 作息必須是完整的 24 小時"
        assert {r["hour"] for r in prof["rows"]} == set(range(24))
        for key in ("own", "site"):
            assert prof[key]["active_hours"] <= 24


def test_peer_ranking_declares_when_it_is_not_the_rules_unit(entity_payloads):
    """母體排名數的是「全部記錄」。有些規則的指標另帶條件（R07A 只算登入失敗、
    R09 只算錯誤），那時兩者不同單位，**畫面必須說**。

    這是本次改版的核心教訓的自動化版本：不同單位的比較不會報錯，
    只會給出一個看起來精確的錯數字。
    """
    for e, p in entity_payloads:
        if not p["supported"]:
            continue
        peers = p["peers"]
        if peers["comparable"]:
            assert peers["note"] is None
            assert abs(peers["own"] - peers["expected"]) < 1
        else:
            assert peers["note"], (
                f"{e['evt_no']} 的排名與事件指標不同單位（own={peers['own']}、"
                f"expected={peers['expected']}）卻沒有任何說明")
            assert str(peers["own"]) in peers["note"].replace(",", "") \
                or f"{peers['own']:,}" in peers["note"]


def test_endpoint_share_says_why_there_is_no_self_share(entity_payloads):
    """對象只有 endpoint（R04）時沒有「自己的佔比」。

    那不是查詢失敗，面板仍然回答「誰在打這個 endpoint」—— 但必須明說，
    否則畫面上一個空白的佔比讀起來像壞掉。
    """
    for e, p in entity_payloads:
        share = p.get("share") if p["supported"] else None
        if share is None:
            continue
        if share["has_self"]:
            assert share["self_note"] is None
            if share["total"]:
                assert share["own_share"] is not None, \
                    f"{e['evt_no']} 有來源維度卻算不出自己的佔比"
        else:
            assert share["own_share"] is None
            assert share["self_note"], f"{e['evt_no']} 沒說明為什麼沒有自己的佔比"


def test_untrackable_entity_degrades_with_a_reason(client):
    """對象不可追蹤（2026-08 政策改版前的 legacy 指紋）時必須明說原因。

    這條路徑的反面是「靜靜回一個空面板」或「退回畫全站流量」，
    而後者正是這次改版要消滅的誤讀來源。
    """
    unsupported = []
    for e in _events(client, limit=20):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if not p["supported"]:
            unsupported.append((e["evt_no"], p))
    for evt_no, p in unsupported:
        assert p.get("reason"), f"{evt_no} 不支援卻沒有給原因"
        assert "peers" not in p, f"{evt_no} 不支援卻仍回傳了母體資料"


def test_timeline_is_contiguous_and_zero_filled(client):
    """分桶必須連續。沒有零填的話空桶直接消失，而 category 軸依索引等距排列 ——
    停掉的那幾天會被壓縮成一段直線而不是凹下去（同 trends 的教訓）。"""
    evt = _events(client, limit=1)[0]["evt_no"]
    r = client.get(f"/api/events/{evt}/entity/timeline?days=4")
    assert r.status_code == 200
    body = r.json()
    if not body["supported"]:
        pytest.skip(f"{evt} 沒有可追蹤的對象")
    rows = body["rows"]
    assert rows, "時序是空的"
    bucket = body["bucket_minutes"]
    assert bucket in {b for _, b in __import__(
        "console.queries.trends", fromlist=["x"]).BUCKET_LADDER}, \
        "分桶必須來自 BUCKET_LADDER，否則與基線粒度不成對"
    from console.core import timewin
    from datetime import timedelta
    times = [timewin.parse(r_["bucket"]) for r_ in rows]
    for a, b in zip(times, times[1:]):
        assert b - a == timedelta(minutes=bucket), "分桶不連續（零填失效）"
    assert all(r_["count"] >= 0 for r_ in rows)


def test_timeline_refuses_to_draw_a_band_without_pre_event_history(client):
    """事件之前樣本不足時 `band` 必須是 None 並說明，不生假的帶。

    與 `sweep/run.build_params()` 同一條規則：用含事件的區間算基線，
    帶會升上來迎合線本身，最重大的事件反而靜靜消失。
    """
    evt = None
    for e in _events(client, limit=20):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if p["supported"]:
            evt = e["evt_no"]
            break
    if evt is None:
        pytest.skip("沒有可追蹤對象的事件")
    # days=2 讓「事件之前」的分桶數低於 MIN_BAND_BUCKETS
    body = client.get(f"/api/events/{evt}/entity/timeline?days=2").json()
    band = body["band"]
    if band["samples"] < entity_history.MIN_BAND_BUCKETS:
        assert band["note"], "樣本不足卻沒說明為什麼沒有帶"
        assert all(r["median"] is None for r in body["rows"]), \
            "沒有帶的時候不可以還帶著 median（前端會照畫）"


def test_timeline_rejects_absurd_day_ranges(client):
    evt = _events(client, limit=1)[0]["evt_no"]
    assert client.get(f"/api/events/{evt}/entity/timeline?days=0").status_code == 422
    assert client.get(f"/api/events/{evt}/entity/timeline?days=9999").status_code == 422


def test_entity_endpoints_404_for_unknown_event(client):
    assert client.get("/api/events/EVT-9999/entity").status_code == 404
    assert client.get("/api/events/EVT-9999/entity/timeline").status_code == 404
