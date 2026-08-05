"""API 煙霧測試（會實際打 ClickHouse，需要有效 .env）。"""
from __future__ import annotations

import pytest


def test_session_reports_identity(client):
    """沒有角色分級：session 回的是身分，不是等級。"""
    r = client.get("/api/session")
    assert r.status_code == 200
    body = r.json()
    assert body["email"]
    assert body["role_label"]          # ROS 的角色名（離線模式為「開發模式」）
    assert body["auth_source"] in ("ros", "dev")
    assert "permissions" not in body, "已移除角色分級，不該再回權限清單"


def test_explorer_rejects_bad_range(client):
    r = client.post("/api/explorer", json={
        "source": "api", "start": "2026-08-01 02:00:00", "end": "2026-08-01 01:00:00"})
    assert r.status_code == 400


def test_explorer_rejects_unknown_source(client):
    r = client.post("/api/explorer", json={
        "source": "system.tables", "start": "2026-08-01 00:00:00",
        "end": "2026-08-01 01:00:00"})
    assert r.status_code == 400


def test_quick_catalog_has_16_templates(client):
    r = client.get("/api/quick")
    cats = r.json()["categories"]
    assert sum(len(c["items"]) for c in cats) == 16
    assert len(cats) == 4


def test_rules_endpoint_lists_all(client):
    r = client.get("/api/rules")
    rules = r.json()["rules"]
    assert len(rules) == 18
    assert {"R01", "R04", "R06", "R12", "R13"} <= {x["id"] for x in rules}


def test_events_list_ok(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    assert "events" in r.json()


def test_event_detail_404(client):
    r = client.get("/api/events/EVT-9999")
    assert r.status_code == 404


def test_judge_accepts_judgement_alone(client):
    """理由／證據／下一步自 2026-08 起皆為選填 —— 只給判定必須成功。

    原本三個都必填，實際結果是大量事件停在「完全沒有判定」；一個空白的理由
    仍然留下了誰、何時、結論是什麼。代價是「沒填」必須說得出來，見下一條斷言。
    """
    r = client.post("/api/events/EVT-0001/judge", json={"judgement": "誤報"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["judgement"] == "誤報"
    assert body["recorded"] == {"reason": "", "evidence": "", "next_step": ""}
    # 「可以不填，但不能安靜」：全空時回應必須明說沒有留下任何理由，
    # 否則畫面上一個什麼都沒寫的判定與一份完整的調查紀錄長得一模一樣。
    assert "沒有留下任何理由" in body["note"]


def test_judge_detail_is_readable_back(client):
    """填了的欄位必須看得到。

    這三個欄位原本是**只寫不讀**的（寫進 judgement_note，而 `_event_public`
    沒有回傳它）。既然改成選填，「有填」就必須與「沒填」看得出差別 ——
    不然選填等於「打了字也沒人會看到」。
    """
    r = client.post("/api/events/EVT-0001/judge",
                    json={"judgement": "合法整合", "reason": "  客戶自家 APP  "})
    assert r.status_code == 200, r.text
    assert r.json()["recorded"]["reason"] == "客戶自家 APP", "前後空白要去掉"
    detail = client.get("/api/events/EVT-0001").json()
    assert detail["judgement_detail"] == {
        "reason": "客戶自家 APP", "evidence": "", "next_step": ""}


def test_judge_rejects_unjudged_as_a_judgement(client):
    """「待判定」是篩選器裡 judgement IS NULL 的顯示值，不是可以提交的判定。"""
    r = client.post("/api/events/EVT-0001/judge", json={"judgement": "待判定"})
    assert r.status_code == 400


def test_judge_rejects_unknown_field(client):
    """沒有 Pydantic：欄名打錯若被靜靜忽略，症狀是「送出成功但什麼都沒存」。"""
    r = client.post("/api/events/EVT-0001/judge",
                    json={"judgement": "誤報", "nextStep": "通知平台團隊"})
    assert r.status_code == 400
    assert "nextStep" in r.json()["detail"]


# ── 圖表相關：時間範圍與 sparkline ────────────────────────────────────────

def test_overview_accepts_seven_day_range(client):
    """前端 RANGES 有「最近 7 天」（minutes=10080）；上限曾是 1440，會 422。"""
    r = client.get("/api/overview?minutes=10080")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trend"]["buckets"], "7 天視窗仍應有趨勢資料"
    # 排名比趨勢貴得多（sources 的 JSONExtract 掃 19M 列），後端會夾在 24 小時
    assert body["rankings"]["window_minutes"] == 1440


def test_overview_ranking_window_matches_when_under_cap(client):
    r = client.get("/api/overview?minutes=360")
    assert r.status_code == 200
    assert r.json()["rankings"]["window_minutes"] == 360


def test_overview_rejects_over_cap(client):
    r = client.get("/api/overview?minutes=20000")
    assert r.status_code == 422


def test_sparklines_shape(client):
    r = client.get("/api/sparklines")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hours"] == 24
    assert set(body["sources"]) == {"admin", "backend", "api", "auth"}
    for key, src in body["sources"].items():
        # 零填過，前端可以直接依索引取用，不必處理缺口
        assert len(src["points"]) == 24, f"{key} 應有 24 個點"
        assert all(isinstance(p["count"], int) for p in src["points"])
    # 嚴重度時間序列做不到（events 表就地覆寫、無逐 tick 歷史），必須誠實回 None
    assert body["severity"] is None
    assert body["severity_note"]


def test_sparklines_available(client):
    """統計卡在總覽與資料健康兩頁都要用到。"""
    r = client.get("/api/sparklines")
    assert r.status_code == 200


# ── 待判定事件 ────────────────────────────────────────────────────────────
# 事件會在數值回到門檻以下時自動 resolved，但首頁的 attention 只查 status='active'，
# 於是自動結束的事件完全從首頁消失、頁面顯示「沒有未處理事件」—— 即使從來沒有人
# 看過它們。那是假的安心感，跟本專案「沒有事件 ≠ 系統安全」的前提正好相反。

def test_overview_reports_pending_judgement(client):
    r = client.get("/api/overview?minutes=60")
    assert r.status_code == 200
    pj = r.json()["pending_judgement"]
    assert set(pj) == {"total", "by_severity", "oldest", "events"}
    assert set(pj["by_severity"]) == {"P0", "P1", "P2", "P3"}
    assert pj["total"] == sum(pj["by_severity"].values())
    assert len(pj["events"]) <= 5
    for e in pj["events"]:
        assert e["judgement"] is None, "待判定清單裡不該出現已判定的事件"
    if pj["total"]:
        assert pj["oldest"], "有待判定事件就該有最早時間，橫幅要用它"


def test_events_unjudged_filter(client):
    """首頁「前往判定」連結指向的查詢。"""
    everything = client.get("/api/events").json()
    unjudged = client.get("/api/events?unjudged=true").json()
    assert all(e["judgement"] is None for e in unjudged["events"])
    assert unjudged["total"] <= everything["total"]
    expected = sum(1 for e in everything["events"] if e["judgement"] is None)
    assert unjudged["total"] == expected


def test_events_unjudged_default_off(client):
    """不帶參數時不可過濾 —— 預設行為不能被這個新參數改掉。"""
    a = client.get("/api/events").json()["total"]
    b = client.get("/api/events?unjudged=false").json()["total"]
    assert a == b


# ── 判定篩選 ──────────────────────────────────────────────────────────────

def test_judgement_tabs_cover_every_judgement(client):
    """每一個判定值都必須屬於某一格頁籤。

    清單頁只剩頁籤這一個入口，所以沒被指派到頁籤的判定值，它的事件會從畫面上
    **完全消失** —— 而畫面看起來完全正常（其他格都有資料）。新增第六個判定值
    卻忘了改 JUDGEMENT_TABS 時，這一條是唯一會出聲的地方。
    """
    d = client.get("/api/events", params={"hours": 24}).json()
    tabs = d["judgement_tabs"]
    keys = [t["key"] for t in tabs]
    assert len(keys) == len(set(keys)), f"頁籤 key 重複：{keys}"
    covered = {j for t in tabs for j in t["judgements"]}
    assert covered == {d["unjudged_label"], *d["judgements"]}, (
        f"有判定值沒有任何頁籤裝得下：{covered ^ {d['unjudged_label'], *d['judgements']}}")
    # 恰好一格是「全部」（空清單 = 不加判定條件）。兩格空的話會有兩個一模一樣
    # 的頁籤，零格則是既有的不分判定入口（總覽 P0 卡片、關鍵字搜尋）沒有落點。
    assert sum(1 for t in tabs if not t["judgements"]) == 1


def test_judgement_tab_count_matches_filter(client):
    """頁籤上的數字必須等於點進去之後的筆數。

    對不上就是「頁籤寫 4，點進去只有 1」，而那個症狀會被讀成資料在跳動。
    total 現在是真實計數（不是 len(events)），所以撞到 LIMIT 300 也照樣可比。
    """
    everything = client.get("/api/events", params={"hours": 2160}).json()
    for tab in everything["judgement_tabs"]:
        got = client.get("/api/events",
                         params={"hours": 2160, "judgement": tab["judgements"]}).json()
        assert got["total"] == tab["count"], (
            f"{tab['label']}：頁籤說 {tab['count']} 筆，點進去 {got['total']} 筆")
        if tab["judgements"] == [everything["unjudged_label"]]:
            assert all(e["judgement"] is None for e in got["events"])
        elif tab["judgements"]:
            assert all(e["judgement"] in tab["judgements"] for e in got["events"])


def test_judgement_tab_counts_ignore_judgement_filter(client):
    """套不套用判定篩選，五格的數字必須一模一樣。

    頁籤的數字回答的是「同樣的條件下，**別格**還有幾筆」—— 它是使用者在一格
    看到 0 筆時唯一的線索。若它跟著判定篩選一起縮，每次點進某一格就會看到
    其餘四格全變 0，等於告訴使用者「別的地方也沒有東西」。
    """
    base = client.get("/api/events", params={"hours": 2160}).json()
    expect = {t["key"]: t["count"] for t in base["judgement_tabs"]}
    for tab in base["judgement_tabs"]:
        got = client.get("/api/events",
                         params={"hours": 2160, "judgement": tab["judgements"]}).json()
        assert {t["key"]: t["count"] for t in got["judgement_tabs"]} == expect, (
            f"在「{tab['label']}」這一格，頁籤數字被判定篩選改掉了")


def test_events_judgement_accepts_multiple(client):
    """judgement 可重複 ——「已排除」那格要一次帶三個值。

    只吃單值的話那一格只能靠前端自己合併三次查詢，而筆數與頁籤數字就會來自
    不同的查詢、不同的時刻。
    """
    picked = ["合法整合", "誤報", "證據不足"]
    got = client.get("/api/events",
                     params={"hours": 2160, "judgement": picked}).json()
    assert all(e["judgement"] in picked for e in got["events"])
    singles = sum(
        client.get("/api/events", params={"hours": 2160, "judgement": j}).json()["total"]
        for j in picked)
    assert got["total"] == singles


def test_events_judgement_rejects_unknown(client):
    """值是封閉集合，打錯要大聲炸。

    靜靜接受的話 `judgement=誤報x` 回 0 筆，而畫面上的已套用條件寫著
    「判定 = 誤報x」—— 讀起來像「這段時間沒有誤報」。
    """
    r = client.get("/api/events", params={"judgement": "誤報x"})
    assert r.status_code == 400
    # 混在合法值裡送也要炸 —— 靜靜忽略的話回來的筆數會少一塊而沒有人知道
    r = client.get("/api/events", params={"judgement": ["誤報", "誤報x"]})
    assert r.status_code == 400


def test_events_judgement_rejects_unjudged_mixed_with_others(client):
    """「待判定」不可與具體判定混用。

    一筆事件不可能同時「還沒有人判定」和「判定是誤報」，混著送的人要的多半是
    別的東西。靜靜回傳兩者聯集的話，「待判定」那格會突然多出已判定的事件。
    """
    r = client.get("/api/events", params={"judgement": ["待判定", "誤報"]})
    assert r.status_code == 400


def test_events_tab_is_shorthand_for_its_judgements(client):
    """tab=<key> 必須等同於把該格成員一個一個列出來。

    這個簡寫存在的理由是**前端不該知道成員清單**：網址是 `#/events?tab=excluded`，
    第一次查詢在拿到 judgement_tabs 之前就要送出。兩者不等價的話，貼網址進來
    與點頁籤進去會是兩個不同的畫面。
    """
    for tab in client.get("/api/events", params={"hours": 24}).json()["judgement_tabs"]:
        by_key = client.get("/api/events",
                            params={"hours": 2160, "tab": tab["key"]}).json()
        by_values = client.get(
            "/api/events",
            params={"hours": 2160, "judgement": tab["judgements"]}).json()
        assert by_key["total"] == by_values["total"], tab["key"]
        assert ([e["evt_no"] for e in by_key["events"]]
                == [e["evt_no"] for e in by_values["events"]]), tab["key"]


def test_events_tab_rejects_unknown_and_mixing(client):
    """tab 也是封閉集合；與 judgement 同時給不定義誰蓋誰，一律 400。

    靜靜退回預設頁籤的話，使用者拿到的是一個**看起來正常、條件卻不是他以為的
    那個**的畫面 —— 網址寫 attck，畫面是待判定，而兩者都沒有出聲。
    """
    assert client.get("/api/events", params={"tab": "attck"}).status_code == 400
    assert client.get("/api/events",
                      params={"tab": "attack", "judgement": "誤報"}).status_code == 400
    assert client.get("/api/events",
                      params={"tab": "attack", "unjudged": "true"}).status_code == 400


def test_events_rejects_unknown_severity_and_source(client):
    """嚴重度與資料來源同樣是封閉集合。

    清單頁的條件現在寫在網址裡、使用者改得到。靜靜回 0 筆配上畫面「嚴重度 = P9」
    讀起來像「這段時間沒有 P9」——「值不存在」與「沒有事件」必須分得開。
    """
    assert client.get("/api/events", params={"severity": "P9"}).status_code == 400
    assert client.get("/api/events", params={"source": "apii"}).status_code == 400
    # 合法值仍要通過（別把驗證寫成什麼都擋）
    assert client.get("/api/events", params={"severity": "P0"}).status_code == 200
    assert client.get("/api/events", params={"source": "api"}).status_code == 200


def test_events_total_is_not_capped_by_limit(client):
    """total 是真實筆數，events 才是被 LIMIT 截斷的那一份。

    兩者混為一談的話，撞到上限時「共 N 筆事件」與四個嚴重度數字會**靜靜少算**，
    而頁籤數字是真實計數 —— 同一個畫面上兩個數字互相打架。
    """
    d = client.get("/api/events", params={"hours": 2160}).json()
    assert d["shown"] == len(d["events"])
    assert d["total"] >= d["shown"]
    assert d["truncated"] is (d["shown"] < d["total"])
    assert sum(d["by_severity"].values()) == d["total"]
    assert sum(d["by_status"].values()) == d["total"]


# ── 人工結案（已處理完畢）────────────────────────────────────────────────
# status 的第三個值只由人寫。狀態機的每一條 SQL 都寫 status='active'，所以
# closed 自動退出狀態機 —— 這幾則守的就是「自動退出」與「不可有假的已恢復」。

def _pick(client, **params):
    r = client.get("/api/events", params={"hours": 2160, **params}).json()
    return r["events"]


def _not_closed(client, evt):
    """確保這一筆不是「已處理完畢」。

    複本 DB 是真實資料的複本，裡面本來就可能有人結案過的事件 ——
    測試不可以假設起始狀態，否則有人在正式環境按了一顆按鈕，
    本機測試就會開始紅（而那個紅燈與程式碼無關）。
    """
    if client.get(f"/api/events/{evt}").json()["status"] == "closed":
        client.post(f"/api/events/{evt}/reopen", json={})


def test_close_requires_a_judgement(client):
    """沒有判定的結案無法回答「處理的結論是什麼」，而且會與首頁的待判定橫幅
    直接矛盾（那條查的是 judgement IS NULL、不看 status）。"""
    unjudged = _pick(client, judgement="待判定")
    if not unjudged:
        pytest.skip("目前沒有未判定的事件")
    r = client.post(f"/api/events/{unjudged[0]['evt_no']}/close", json={})
    assert r.status_code == 400
    assert "判定" in r.json()["detail"]


def test_close_and_reopen_round_trip(client):
    """結案 → 狀態變 closed 且退出狀態機；復原 → 回到關閉當下的值。"""
    evt = "EVT-0001"
    _not_closed(client, evt)
    judged = client.post(f"/api/events/{evt}/judge", json={"judgement": "誤報"})
    assert judged.status_code == 200, judged.text
    before = client.get(f"/api/events/{evt}").json()["status"]

    closed = client.post(f"/api/events/{evt}/close", json={"reason": "已通知平台團隊"})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "closed"
    assert body["closed_from"] == before
    assert body["closed_by"]
    detail = client.get(f"/api/events/{evt}").json()
    assert detail["status"] == "closed"
    assert detail["closed_at"] and detail["closed_by"]

    # 篩選得到、而且不再被算進 active
    assert evt in {e["evt_no"] for e in _pick(client, status="closed")}
    assert evt not in {e["evt_no"] for e in _pick(client, status="active")}

    # 復原一律回到 closed_from，**不可一律回 active** —— 一筆早就回落的事件被
    # 復原成 active 之後，狀態機會在三個 tick 內發一則假的「已恢復」。
    reopened = client.post(f"/api/events/{evt}/reopen", json={})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == before
    after = client.get(f"/api/events/{evt}").json()
    assert after["status"] == before
    assert after["closed_at"] is None and after["closed_from"] is None


def test_close_twice_is_409(client):
    """回 200 + changed:false 的話畫面會顯示「已標記完成」而什麼都沒發生。"""
    evt = "EVT-0001"
    _not_closed(client, evt)
    client.post(f"/api/events/{evt}/judge", json={"judgement": "誤報"})
    first = client.post(f"/api/events/{evt}/close", json={})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/events/{evt}/close", json={})
    assert second.status_code == 409
    client.post(f"/api/events/{evt}/reopen", json={})       # 還原給其他測試


def test_reopen_when_not_closed_is_409(client):
    evt = "EVT-0001"
    _not_closed(client, evt)
    assert client.get(f"/api/events/{evt}").json()["status"] != "closed"
    r = client.post(f"/api/events/{evt}/reopen", json={})
    assert r.status_code == 409


def test_closing_an_active_event_warns_about_the_blind_spot(client):
    """關閉仍在命中的事件是允許的，但不可以安靜。

    它會從「持續中」與資安總覽的待處理清單消失（兩處都查 status='active'），
    而下一個檢查視窗若仍然命中，狀態機找不到 active 列會另開一個新的 EVT 編號。
    兩件事都必須在回應的 warnings 裡講出來 —— 前端原樣顯示。

    複本 DB 裡不一定有 active 的事件（狀態機隨時會把它們標 resolved），
    所以這裡自己造一個；conftest 的 DB 是 tmp 複本，改它是安全的。
    """
    from console.store import db

    evt = "EVT-0001"
    _not_closed(client, evt)
    client.post(f"/api/events/{evt}/judge", json={"judgement": "誤報"})
    before = db.one("SELECT status FROM events WHERE evt_no = ?", (evt,))["status"]
    with db.tx() as conn:
        conn.execute("UPDATE events SET status = 'active' WHERE evt_no = ?", (evt,))
    try:
        r = client.post(f"/api/events/{evt}/close", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["closed_from"] == "active"
        assert body["warnings"], "關閉仍在命中的事件必須帶警告"
        assert "新的事件編號" in " ".join(body["warnings"])
        # 復原要回到 active（關閉當下的值），不是一律回 resolved
        assert client.post(f"/api/events/{evt}/reopen", json={}).json()["status"] == "active"
    finally:
        with db.tx() as conn:
            conn.execute("UPDATE events SET status = ?, closed_at = NULL,"
                         " closed_by = NULL, closed_from = NULL WHERE evt_no = ?",
                         (before, evt))


def test_closing_a_settled_event_has_no_warning(client):
    """回落之後才結案沒有盲區，就不該掛一則警告 —— 每次都警告等於沒有警告。"""
    evt = "EVT-0001"
    _not_closed(client, evt)
    client.post(f"/api/events/{evt}/judge", json={"judgement": "誤報"})
    if client.get(f"/api/events/{evt}").json()["status"] != "resolved":
        pytest.skip("EVT-0001 目前不是「已恢復」，這則測試沒有可驗的對象")
    r = client.post(f"/api/events/{evt}/close", json={})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
    client.post(f"/api/events/{evt}/reopen", json={})


def test_events_status_rejects_unknown(client):
    """status 也是封閉集合 —— 多了 closed 之後打錯的機會變高。"""
    r = client.get("/api/events", params={"status": "closd"})
    assert r.status_code == 400
    for ok in ("active", "resolved", "closed"):
        assert client.get("/api/events", params={"status": ok}).status_code == 200


def test_events_judgement_conflict_with_unjudged(client):
    """unjudged 與具體判定同時給永遠是 0 筆，那必須是 400 而不是空清單。"""
    r = client.get("/api/events", params={"judgement": "誤報", "unjudged": "true"})
    assert r.status_code == 400
    # 「待判定」與 unjudged 指的是同一件事，不算矛盾（總覽的連結會走到這裡）
    same = client.get("/api/events", params={"judgement": "待判定", "unjudged": "true"})
    assert same.status_code == 200, same.text
    assert all(e["judgement"] is None for e in same.json()["events"])


# ── 自適應分桶 ────────────────────────────────────────────────────────────
# 固定 10 分鐘分桶時「最近 1 小時」只有 6 個點、「最近 7 天」有 1008 個點。
# 桶數對不上預期通常代表 timewin.align_bucket 與 ClickHouse 的格線錯位 ——
# 那個 bug 不會報錯，只會讓整張圖靜靜變成一條 0，所以這裡連桶數一起驗。

def test_bucket_ladder_one_hour(client):
    t = client.get("/api/overview?minutes=60").json()["trend"]
    assert t["bucket_minutes"] == 5
    assert len(t["buckets"]) == 12


def test_bucket_ladder_seven_days(client):
    t = client.get("/api/overview?minutes=10080").json()["trend"]
    assert t["bucket_minutes"] == 120
    assert len(t["buckets"]) == 84


def test_wide_window_still_has_baselines_and_sane_multiples(client):
    """分桶變粗時基線也要跟著換粒度，否則會冒出假的 12 倍。"""
    buckets = client.get("/api/overview?minutes=10080").json()["trend"]["buckets"]
    for name in ("api", "backend", "login_success", "login_failed"):
        assert any(b[f"{name}_median"] is not None for b in buckets), (
            f"{name} 在 120 分鐘分桶下沒有基線 —— calibrate 是否漏算這個粒度？")
    mults = [b["api_multiple"] for b in buckets if b["api_multiple"] is not None]
    assert mults, "應該要有倍數"
    # 用 10 分鐘的基線比 120 分鐘的桶會系統性地放大約 12 倍；
    # 這個上限抓得寬鬆，只是要擋掉「整段都被放大」那種粒度錯配。
    assert sum(m > 12 for m in mults) < len(mults) / 2, (
        f"超過一半的桶倍數 > 12，疑似基線粒度與分桶不匹配：{mults[:8]}")


def test_no_all_zero_trend_from_grid_misalignment(client):
    """格線錯位的症狀就是每個桶都查不到資料 → 整段變成 0。"""
    buckets = client.get("/api/overview?minutes=10080").json()["trend"]["buckets"]
    assert sum(b["api"] for b in buckets) > 0, "7 天視窗的 API 總量不可能是 0"


def test_event_trend_padding_widens_the_window(client):
    """事件視窗常常只有一兩小時，只看前後 30 分鐘看不出事件之前的脈絡。"""
    narrow = client.get("/api/events/EVT-0001?pad_minutes=30").json()["trend"]
    wide = client.get("/api/events/EVT-0001?pad_minutes=720").json()["trend"]
    assert narrow["rows"] and wide["rows"]
    # 拉寬之後分桶會變粗，但涵蓋的時間一定更長
    assert wide["bucket_minutes"] >= narrow["bucket_minutes"]
    span = lambda t: len(t["rows"]) * t["bucket_minutes"]
    assert span(wide) > span(narrow)


def test_event_trend_does_not_extend_into_the_future(client):
    """往後拉的區間很容易超過現在，那段永遠是 0，看起來像流量歸零。"""
    from console.core import timewin
    rows = client.get("/api/events/EVT-0001?pad_minutes=2880").json()["trend"]["rows"]
    now = timewin.effective_now()
    for r in rows:
        # bucket 是 "%m/%d %H:%M"，補上年份再比（事件都在今年）
        stamp = f"{now.year}/{r['bucket']}"
        assert stamp <= now.strftime("%Y/%m/%d %H:%M"), f"{r['bucket']} 在未來"


def test_event_trend_is_zero_filled(client):
    """沒有零填的話空桶會消失，category 軸依索引等距排列，安靜時段會被壓縮成直線。"""
    t = client.get("/api/events/EVT-0001?pad_minutes=180").json()["trend"]
    from console.core import timewin
    b = t["bucket_minutes"]
    stamps = [timewin.parse(f"2026/{r['bucket']}".replace("/", "-", 1)
                            .replace("/", "-", 1)) for r in t["rows"]]
    gaps = {int((b2 - b1).total_seconds() // 60) for b1, b2 in zip(stamps, stamps[1:])}
    assert gaps <= {b}, f"桶之間應等距 {b} 分鐘，實際有 {gaps}"


# ── 自訂絕對區間 ──────────────────────────────────────────────────────────
# 自訂區間會產生任意長度的視窗（例如 13:07~09:23），正是以前「今天」害整張圖
# 變成一條 0 的那個情境 —— start 沒對齊分桶格線。這組測試守著它。

def test_custom_range_overview(client):
    r = client.get("/api/overview",
                   params={"start": "2026-07-16 00:00:00", "end": "2026-07-16 06:00:00"})
    assert r.status_code == 200, r.text
    t = r.json()["trend"]
    assert t["start"] == "2026-07-16 00:00:00"
    assert t["end"] == "2026-07-16 06:00:00"
    assert sum(b["api"] for b in t["buckets"]) > 0, "自訂區間整段都是 0 —— start 沒對齊？"


def test_custom_range_with_odd_boundaries_is_aligned(client):
    """邊界故意不落在格線上，回傳的每個桶起點仍必須對齊。"""
    from console.core import timewin
    r = client.get("/api/overview",
                   params={"start": "2026-08-01 13:07:00", "end": "2026-08-02 09:23:00"})
    assert r.status_code == 200
    t = r.json()["trend"]
    b = t["bucket_minutes"]
    for row in t["buckets"]:
        dt = timewin.parse(row["bucket"])
        assert timewin.align_bucket(dt, b) == dt, f"{row['bucket']} 不在 {b} 分鐘格線上"
    assert sum(x["api"] for x in t["buckets"]) > 0


def test_custom_range_validation(client):
    bad = [
        ({"start": "2026-08-02 10:00:00", "end": "2026-08-02 09:00:00"}, "start 晚於 end"),
        ({"start": "2026-01-01 00:00:00", "end": "2026-08-01 00:00:00"}, "超過上限天數"),
        ({"start": "不是時間", "end": "2026-08-02 09:00:00"}, "無法解析"),
    ]
    for params, why in bad:
        assert client.get("/api/overview", params=params).status_code == 400, why
    # 只給一半視同沒給，回退成 minutes
    assert client.get("/api/overview", params={"start": "2026-08-02 09:00:00"}).status_code == 200


def test_custom_range_event_trend(client):
    r = client.get("/api/events/EVT-0001",
                   params={"start": "2026-08-01 00:00:00", "end": "2026-08-03 00:00:00"})
    assert r.status_code == 200
    rows = r.json()["trend"]["rows"]
    assert rows and rows[0]["bucket"].startswith("08/01")


def test_three_day_preset(client):
    """新的「最近 3 天」= 4320 分鐘，應落在 120 分鐘分桶、36 個點。"""
    t = client.get("/api/overview?minutes=4320").json()["trend"]
    assert t["bucket_minutes"] == 120
    assert len(t["buckets"]) == 36


def test_whole_day_range_is_not_truncated(client):
    """自訂區間只選日期，結束一律是 23:59:59。

    右界必須**向上**取整到完整的桶 —— 向下取整的話 120 分鐘分桶會退到 22:00，
    當天最後兩小時整段消失，而且不會有任何錯誤訊息。
    """
    t = client.get("/api/overview",
                   params={"start": "2026-07-16 00:00:00",
                           "end": "2026-07-16 23:59:59"}).json()["trend"]
    b = t["bucket_minutes"]
    assert len(t["buckets"]) == 1440 // b, (
        f"整天應該有 {1440 // b} 個 {b} 分鐘的桶，實際 {len(t['buckets'])} 個")
    assert t["start"] == "2026-07-16 00:00:00"
    assert t["end"] == "2026-07-17 00:00:00", "右界要涵蓋到當天最後一刻"


def test_multi_day_range_is_not_truncated(client):
    t = client.get("/api/overview",
                   params={"start": "2026-07-15 00:00:00",
                           "end": "2026-07-17 23:59:59"}).json()["trend"]
    b = t["bucket_minutes"]
    assert len(t["buckets"]) == 3 * 1440 // b
    assert t["end"] == "2026-07-18 00:00:00"


# ─────────────────────── 品牌選擇器（GET /api/brands）───────────────────────

def test_brands_endpoint_searches_by_name(client):
    rows = client.get("/api/brands", params={"q": "瓦城"}).json()["rows"]
    assert rows
    assert all("idx" in b and "name" in b and "status" in b for b in rows)


def test_brands_endpoint_searches_by_id(client):
    rows = client.get("/api/brands", params={"q": "1180"}).json()["rows"]
    assert rows[0]["idx"] == 1180


def test_brands_endpoint_blank_query_is_empty(client):
    assert client.get("/api/brands", params={"q": ""}).json()["rows"] == []
    assert client.get("/api/brands").json()["rows"] == []


def test_brands_endpoint_rejects_out_of_range_limit(client):
    assert client.get("/api/brands", params={"q": "a", "limit": 0}).status_code == 422
    assert client.get("/api/brands", params={"q": "a", "limit": 999}).status_code == 422


def test_brands_endpoint_respects_limit(client):
    rows = client.get("/api/brands", params={"q": "a", "limit": 5}).json()["rows"]
    assert len(rows) <= 5


# ───────────────────── endpoint 建議（GET /api/endpoints）─────────────────────

_EP_WIN = {"start": "2026-08-01 00:00:00", "end": "2026-08-02 00:00:00"}


def test_endpoints_endpoint_returns_sorted_rows(client):
    body = client.get("/api/endpoints", params={"source": "api", **_EP_WIN}).json()
    counts = [r["count"] for r in body["rows"]]
    assert counts and counts == sorted(counts, reverse=True)
    assert body["total"] == len(body["rows"])


def test_endpoints_endpoint_rejects_auth_with_400(client):
    """迴歸：ods_auth_log 沒有 function 欄位，以前這條路徑會生出壞 SQL 回 502。"""
    r = client.get("/api/endpoints", params={"source": "auth", **_EP_WIN})
    assert r.status_code == 400, r.text


def test_endpoints_endpoint_rejects_bad_window(client):
    r = client.get("/api/endpoints", params={
        "source": "api", "start": "2026-08-02 00:00:00", "end": "2026-08-01 00:00:00"})
    assert r.status_code == 400


def test_endpoints_endpoint_requires_window(client):
    assert client.get("/api/endpoints", params={"source": "api"}).status_code == 400


def test_explorer_auth_endpoint_filter_is_400_not_502(client):
    """同一個 bug 的另一個入口。"""
    r = client.post("/api/explorer", json={
        "source": "auth", "analysis": "trend", "endpoint": "login", **_EP_WIN})
    assert r.status_code == 400, r.text
