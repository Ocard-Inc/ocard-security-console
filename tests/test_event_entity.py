"""對象視角面板（`queries/entity.py` / `entity_history.py` 與兩個端點）。

這個檔案守的是**單位**與**降級**兩件事，因為兩者壞掉時都不會報錯：

- 單位：母體排名若與事件指標不同單位，畫面會顯示一個精確的錯數字。
  這正是催生這次改版的 bug —— R03 的 metric 是 per (src, endpoint)，
  而它的基線是 per src，實測 P99 差 26 倍。
- 降級：對象不可追蹤時必須明說，**不可以退回畫全站圖假裝有內容**。
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from console.core import brands, masking, stores, timewin
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


def test_with_values_swaps_values_and_keeps_the_dimensions():
    """點母體排名的第 N 列 → 用那一列的值組一個新的 EntityRef。

    `EntityRef`／`Dim` 是 frozen dataclass，所以這裡一定是產生新物件 ——
    就地改掉的話會污染同一個請求裡其他面板用的 ref（同 `rules/effective` 用
    `dataclasses.replace()` 的理由）。
    """
    ref = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                      "endpoint": "Api2/GetProfile"})
    other = entity.with_values(ref, ["5.6.7.8", "Api2/Login"])

    assert [d.value for d in other.dims] == ["5.6.7.8", "Api2/Login"]
    # 維度定義（欄位、運算式、遮罩、名稱）完全不變 —— 只有值換了
    assert [(d.field, d.expr, d.mask) for d in other.dims] == \
           [(d.field, d.expr, d.mask) for d in ref.dims]
    # 原物件沒有被就地改掉
    assert [d.value for d in ref.dims] == ["1.2.3.4", "Api2/GetProfile"]

    # 個數不符要拋，不可以靜靜少比一個維度 —— 那會組出一個範圍更大的對象，
    # 數字比左邊那根長條大而且不會有任何錯誤
    with pytest.raises(ValueError):
        entity.with_values(ref, ["5.6.7.8"])
    with pytest.raises(ValueError):
        entity.with_values(ref, ["5.6.7.8", "Api2/Login", "多的"])


def test_breakdown_fields_excludes_what_is_already_the_ranking_unit():
    """拆解維度 = 四個候選減掉「已經被拿去排序的」。

    對 (來源 IP × endpoint) 的對象再按 endpoint 拆只會得到一列 —— 那不是資訊，
    而是一塊看起來壞掉的面板。順序固定成「打什麼 → 誰 → 影響誰」，
    同一條規則的事件每次讀起來才一樣。
    """
    both = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                       "endpoint": "Api2/GetProfile"})
    assert entity.breakdown_fields(both) == ["actor", "brand", "store"]

    only_src = entity.from_filters("api", {"source_ip": "1.2.3.4"})
    assert entity.breakdown_fields(only_src) == \
        ["endpoint", "actor", "brand", "store"]

    # auth 的 endpoint 是 `action`、actor 是 API token，兩者都有運算式，
    # 所以四個維度都在 —— 「能不能拿來反查」是 filter_support() 的事，
    # 這裡只問「這張表有沒有這個分組運算式」（拆解只是分組顯示，
    # 而 token 的指紋當標籤是正確的呈現）。
    auth = entity.from_filters("auth", {"source_ip": "1.2.3.4"})
    assert entity.breakdown_fields(auth) == \
        ["endpoint", "actor", "brand", "store"]


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


def test_entity_condition_that_matches_nothing_degrades_instead_of_lying(client):
    """事件正在命中，而對象條件比對到 0 筆 → 那是**運算式不成對**，
    不是「這個對象很安靜」。整塊面板必須降級並說出原因。

    實例（2026-08-05 由 EVT-0052 暴露）：R06 的 entity 值是規則 SQL 裡的字面常數
    `Boss_initial/auth_v2`，而 admin 的 endpoint 母體鍵是
    `concat(function, '/', action)` —— 值有三段，完全相等比對永遠 0 筆。
    `entity.py` 的模組說明假設「事件的 entity 值就是 `entity_expr()` 算出來的」，
    字面常數的規則違反這個假設，而**沒有任何執行期檢查**。

    症狀是三塊面板各自編出一個不同的、看起來合理的錯誤說法：
      - 母體位置：`above` 數到 9 → rank=10 而 groups=9（排名比母體還大），
        且 `comparable=False` 的說明把原因誤指為「規則指標另外帶了條件」；
      - 24 小時作息：`own_total=0` → 「此區間內沒有任何活動」，
        而那個對象正在告警；
      - 端點集中度：全 0。

    `own < expected` 是**矛盾**（總數不可能小於它的子集），所以這個檢查不需要
    知道任何規則的細節，也不必為 R06 寫特例。
    """
    checked = 0
    for e in _events(client, limit=25):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if not p["supported"]:
            assert p.get("reason"), f"{e['evt_no']} 不支援卻沒有給原因"
            assert "peers" not in p
            continue
        checked += 1
        peers, where = p["peers"], f"{e['evt_no']}（{e.get('rule_id')}）"
        assert peers["own"] >= 1, (
            f"{where} 事件正在命中（metric={e.get('metric_value')}）而對象條件"
            f"比對到 {peers['own']} 筆 —— 比對運算式與事件的 entity 值不成對，"
            "面板必須整塊降級並說出原因，不可以給假排名或說「沒有任何活動」")
        assert peers["own"] >= (peers["expected"] or 0) - 1, (
            f"{where} own={peers['own']} 小於事件指標 {peers['expected']} —— "
            "總數不可能小於它的子集，這是對象條件錯了")
        assert 1 <= peers["rank"] <= peers["groups"], f"{where} 排名超出母體範圍"
        assert p["profile"]["own_total"] >= 1, \
            f"{where} 作息說對象完全沒有活動，而它正在告警"
    assert checked, "沒有任何 supported 的事件，這個測試等於空跑"


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


# --- 母體位置的品牌／分店名稱 -------------------------------------------------

# 品牌 1180「wa10 瓦城」/ 分店 27681「WA10 APP」：R13 的持續高量對象，
# 2026-08-01 12:00–13:00 有 12,981 次，穩定落在前 12 名內。
_NAMED = {"brand": 1180, "store": 27681}
_NAMED_WINDOW = ("2026-08-01 12:00:00", "2026-08-01 13:00:00")


def _named_peers():
    ref = entity.from_filters("api", _NAMED)
    assert ref is not None
    start, end = (timewin.parse(s) for s in _NAMED_WINDOW)
    return entity.peers(ref, start, end)


def test_peer_labels_name_the_brand_and_store():
    """母體位置的每一列都要看得懂是誰。

    橫條圖與底下的表格都直接渲染 `top[].label`（見 web/components/entity-panels.js），
    而 `_display()` 對 mask 為 None 的維度原樣回傳 —— 品牌與分店因此是裸編號。
    「1180 · 27681」沒有人認得出是「wa10 瓦城 · WA10 APP」，而這一塊的用途正是
    讓人一眼看出離群的是誰。事件標題早就解名稱了（engine.entity_parts），
    同一頁的兩處不一致本身就是缺陷。
    """
    result = _named_peers()
    assert result["top"], "這個區間應該有母體資料"

    self_row = next((r for r in result["top"] if r["is_self"]), None)
    assert self_row is not None, (
        f"本對象不在前 {len(result['top'])} 名內，換一個區間或對象："
        f"{[r['label'] for r in result['top']]}")
    assert brands.label(_NAMED["brand"]) in self_row["label"], self_row["label"]
    assert stores.label(_NAMED["store"]) in self_row["label"], self_row["label"]

    bare = [r["label"] for r in result["top"] if "（" not in r["label"]]
    assert not bare, f"這些列仍是裸編號：{bare}"


def test_peer_self_detection_still_uses_the_raw_values():
    """加了名稱之後，`is_self` 不可以改用標籤比對。

    店名會改，而且「（查無分店）」會讓多列長得一模一樣 —— 用標籤比對的話
    高亮會落在錯的長條上，或者一次亮好幾條，而畫面看起來完全正常。
    """
    result = _named_peers()
    assert sum(1 for r in result["top"] if r["is_self"]) == 1, (
        "本對象必須剛好命中一列")


def test_peer_rows_carry_raw_keys_only_when_they_are_echoable():
    """母體排名的每一列都要能被點來往下拆，而那需要**原始值**。

    `keys` 的順序同 `ref.dims`，而且**只在可回送時才給** —— `auth` 的 actor 是
    API token，畫面上是不可逆指紋，回送等於用主控台把它還原
    （閘門是 `masking.echoable()`，見 tests/test_masking_audit.py）。

    不可回送時給 `None` 而不是省略這個鍵：前端要能分辨「這一列點不動」與
    「後端還是舊版、沒有這個功能」（後者整個 top 都沒有 `keys` 這個鍵，
    前端據此把整塊降級成唯讀）。
    """
    ref = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                      "endpoint": "Api2/GetProfile"})
    assert ref is not None
    start, end = (timewin.parse(s) for s in _NAMED_WINDOW)
    out = entity.peers(ref, start, end)
    assert out["top"], "這個區間應該有母體資料"

    for row in out["top"]:
        assert "keys" in row, "每一列都必須有 keys 鍵（不可回送時是 None）"
        assert row["keys"] is not None, (
            f"來源 IP 與 endpoint 在 2026-08 的政策下都是原樣顯示，"
            f"應該可回送：{row['label']}")
        assert len(row["keys"]) == len(ref.dims), (
            "keys 的長度必須等於維度數，否則回送後組出的 WHERE 範圍更大，"
            "數字會比那根長條大而且不會有任何錯誤")
        # 這兩個維度都是原樣顯示，所以逐段串起來就是 label
        assert " · ".join(row["keys"]) == row["label"]
        for dim, value in zip(ref.dims, row["keys"]):
            assert masking.echoable(dim.mask, value)


def test_peer_keys_are_the_raw_values_not_the_named_labels():
    """品牌與分店的 label 是「名稱（編號）」，但 `keys` 必須是**裸編號**。

    回送 label 的話後端拿「wa10 瓦城（1180）」去比對 `toString(_brand)`
    永遠 0 筆 —— 而畫面會顯示一個空的拆解面板，看起來像這個對象沒有活動。
    """
    result = _named_peers()
    self_row = next((r for r in result["top"] if r["is_self"]), None)
    assert self_row is not None, "本對象必須命中一列"
    assert self_row["keys"] == [str(_NAMED["brand"]), str(_NAMED["store"])]
    # 反向：label 確實已經解過名稱（兩者刻意不同）
    assert self_row["keys"] != [self_row["label"]]
    assert brands.label(_NAMED["brand"]) in self_row["label"]


def test_breakdown_accounts_for_every_record_it_did_not_show():
    """拆解的前 N 名加不到 100% 時，畫面要說得出剩下的去哪了。

    `blank`（該維度是空字串的筆數）**一定要回**：不回的話「沒有帳號的那些筆」
    會靜靜藏在分母裡，而佔比看起來只是「剛好不到 100%」。
    這是這個專案一再警告的「把沒有資料說成沒有發生」的同一種錯。
    """
    ref = entity.from_filters("api", {"endpoint": "Api2/GetProfile"})
    assert ref is not None
    start, end = (timewin.parse(s) for s in _NAMED_WINDOW)
    out = entity.breakdown(ref, start, end)

    assert out["total"] > 0, "這個時段這個 endpoint 沒有資料，換一個已知有量的"
    # endpoint 已經是排序單位，所以不會出現在拆解裡
    assert [d["field"] for d in out["dims"]] == ["actor", "brand", "store"]
    assert out["note"] is None

    for d in out["dims"]:
        where = d["field"]
        assert d["label"], f"{where} 少了顯示名稱"
        assert len(d["rows"]) <= entity.BREAKDOWN_LIMIT
        # 相異值個數不可能少於列出的名次 —— 反了就是母體與明細不同來源
        assert d["groups"] >= len(d["rows"]), where
        # 前 N 名 + 空值 不可能超過總數（前 N 名刻意排除空值那一組）
        assert sum(r["count"] for r in d["rows"]) + d["blank"] <= out["total"], where
        # 由高到低
        assert [r["count"] for r in d["rows"]] == \
            sorted((r["count"] for r in d["rows"]), reverse=True), where
        for r in d["rows"]:
            # 比例一律是小數（0..1）。回百分比的話前端的 pct() 會再乘 100
            # ——實測 97.47 顯示成 9747.0%
            assert 0 <= r["share"] <= 1, f"{where} 的 share 不是小數"
            assert r["label"], f"{where} 有一列沒有標籤"
            # 原始值不可以出現在拆解裡（這一層不再往下鑽，不需要它，
            # 而 auth 的 actor 原始值是有效憑證）
            assert "value" not in r, f"{where} 洩漏了原始值"


def test_breakdown_says_so_when_there_is_nothing_left_to_split():
    """四個維度全部被拿去排序時，回空清單 + 一句說明，不是一塊空白面板。"""
    ref = entity.from_filters("api", {
        "source_ip": "1.2.3.4", "endpoint": "Api2/GetProfile",
        "actor": "andrew_c", "brand": "1180", "store": "27681"})
    assert ref is not None
    start, end = (timewin.parse(s) for s in _NAMED_WINDOW)
    out = entity.breakdown(ref, start, end)
    assert out["dims"] == []
    assert out["note"], "沒有可拆維度時必須說出原因"


# --- 選中對象的兩個端點（拆解、趨勢）-----------------------------------------

def _first_supported(client) -> tuple[dict, dict]:
    """第一個「對象可追蹤」的事件與它的對象面板回應。"""
    for e in _events(client):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if p.get("supported"):
            return e, p
    pytest.skip("DB 裡沒有對象可追蹤的事件")


def _picked(payload: dict) -> dict | None:
    """母體排名裡第一個可回送的列。"""
    return next((r for r in payload["peers"]["top"] if r["keys"]), None)


def _vq(keys: list[str]) -> str:
    return "&".join(f"v={quote(v, safe='')}" for v in keys)


def test_breakdown_endpoint_defaults_to_the_events_own_object(client):
    """`v` 省略 = 本事件的對象。

    預設載入**不可以**依賴 `keys`：本事件的對象可能根本不在前 12 名裡，
    那時前端手上沒有任何可回送的值。
    """
    e, p = _first_supported(client)
    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["supported"] is True
    assert d["is_self"] is True
    assert d["label"] == p["label"], "預設對象必須就是面板標頭那一個"
    # 與 peers 同一個區間、同一個對象，所以總數必須一致 —— 不一致就是兩邊的
    # 視窗或條件漂移了，而那會讓左邊的長條與右邊的拆解對不起來
    assert d["total"] == p["peers"]["own"]


def test_breakdown_endpoint_follows_a_selected_peer(client):
    """點母體排名的任一列 → 拆解跟著換對象。"""
    e, p = _first_supported(client)
    picked = _picked(p)
    if picked is None:
        pytest.skip("這個事件的母體沒有可回送的列（例如 auth 的 token 對象）")

    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?{_vq(picked['keys'])}")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["label"] == picked["label"], "換了對象但標頭沒跟著換"
    assert d["is_self"] is picked["is_self"]
    # 拆解的總數必須等於那一列長條的長度，否則畫面上兩者對不起來
    assert d["total"] == picked["count"]


def test_breakdown_endpoint_rejects_a_wrong_number_of_values(client):
    """`v` 的個數與維度數不符一律 400。

    少一個維度就是在查一個**範圍更大**的對象 —— 數字會比那根長條大，
    而且不會有任何錯誤訊息。
    """
    e, p = _first_supported(client)
    n = len(p["dims"])
    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?"
                   + "&".join(["v=x"] * (n + 1)))
    assert r.status_code == 400, r.text[:300]
    if n > 1:
        r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?v=x")
        assert r.status_code == 400, r.text[:300]


def test_breakdown_endpoint_404_for_unknown_event(client):
    assert client.get("/api/events/EVT-9999/entity/breakdown").status_code == 404


def test_trend_endpoint_defaults_to_the_events_own_object(client):
    """趨勢預設畫本事件的對象，且錨點是事件的 last_seen 而不是現在。

    用 `now()` 的話同一個事件在隔天會變成一張與它無關的圖，而且不會報錯 ——
    所以右界必須貼著 `last_seen`（同一個桶內）。
    """
    e, p = _first_supported(client)
    r = client.get(f"/api/events/{e['evt_no']}/entity/trend?minutes=1440")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["supported"] is True
    assert d["is_self"] is True
    assert d["label"] == p["label"]
    assert len(d["rows"]) == 1440 // d["bucket_minutes"]
    # 區間清單與「較慢」標註都由後端給，前端不列第二份
    assert sorted(d["ranges"]) == sorted(entity_history.TREND_RANGES)
    assert set(d["slow_ranges"]) <= set(d["ranges"]), \
        "被標成較慢的區間必須真的是可選的區間，否則警語永遠不出現"

    # 錨點貼著 last_seen（除非被夾到已落地的資料，那時要有 window_note）
    last = timewin.parse(e["last_seen"])
    anchor = timewin.parse(d["anchor"])
    if not d["window_note"]:
        gap = (anchor - last).total_seconds()
        assert 0 < gap <= d["bucket_minutes"] * 60, \
            f"錨點沒有貼著事件的 last_seen（差 {gap} 秒）"


def test_trend_endpoint_rejects_a_range_outside_the_closed_set(client):
    """`minutes` 是封閉集合，打錯一律 400。

    靜靜挑一個分桶的話畫面會寫「最近 5 小時」而圖是別的長度 ——
    「值不存在」與「這段時間沒有活動」必須分得開。
    """
    e, _ = _first_supported(client)
    for bad in (300, 0, -60, 999999):
        r = client.get(f"/api/events/{e['evt_no']}/entity/trend?minutes={bad}")
        assert r.status_code == 400, f"minutes={bad} → {r.status_code}"


def test_trend_endpoint_follows_a_selected_peer(client):
    """點母體排名的任一列 → 趨勢跟著換對象。"""
    e, p = _first_supported(client)
    picked = _picked(p)
    if picked is None:
        pytest.skip("這個事件的母體沒有可回送的列")
    r = client.get(f"/api/events/{e['evt_no']}/entity/trend"
                   f"?minutes=60&{_vq(picked['keys'])}")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["label"] == picked["label"]
    assert d["is_self"] is picked["is_self"]
