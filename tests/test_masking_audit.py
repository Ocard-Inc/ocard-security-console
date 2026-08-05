"""端點層級的呈現政策稽核：掃描各 API 的實際回應。

政策見 `core/masking.py`。這個檔案在改政策時**一起改過**——它原本斷言
「任何回應都不得出現帳號與 IP」，那條規則讓主控台無法完成它唯一的任務
（追究問題出在哪個帳號、哪個來源），已由使用者明確決定移除。

現在守的是兩件事：

1. **不該外流的沒有外流**：消費者手機與 Email、payload 裡的憑證值、
   有效的 API token。這些不是調查對象，而且它們的去向不只畫面 ——
   `alerting/notify.py` 會送進 Slack，應用 log 明文寫在 `state/logs/*.log`。
2. **該顯示的真的顯示**：帳號與 IP 必須出現在掃描與 Explorer 的回應裡。
   少了這條，未來有人「順手」把遮罩加回去不會有任何測試失敗，
   而工具會靜靜地退回無法追究問題的狀態。
"""
from __future__ import annotations

import re

# 消費者個資樣式：台灣手機、Email
PHONE = re.compile(r"\b09\d{8}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# 操作者自己的 email 是刻意留痕的（audit_log.who、sweeps.created_by、session 端點），
# 不屬於消費者個資。dev@ 是離線模式下 X-Dev-User 的預設值。
EMAIL_ALLOW = {"vinek@olis.com.tw", "dev@olis.com.tw"}

# payload 裡出現這些字樣代表憑證值沒有被清洗掉。
# 值本身是隨機的，所以檢查的是「鍵後面直接跟著一段非 *** 的值」。
CREDENTIAL_LEAK = re.compile(
    r'(?i)"(?:authorization|cookie|password|pwd|secret|api[_-]?key|vtoken)"\s*:\s*"(?!\*\*\*)[^"]{6,}"'
)


# 「操作者是誰」的欄位。這些欄位裡的 Email 是**刻意留痕**，不是洩漏 ——
# audit_log 的 who、Allowlist 的核准人與負責人、規則覆寫的操作者。
#
# 掃描前先把這些鍵整個移除，再檢查剩下的字串。**這是結構性豁免，
# 不是放寬 EMAIL_ALLOW。** 那個集合只有兩個位址，而正式環境有更多真人；
# 把他們加進白名單（或放寬 EMAIL regex）會稀釋整條防線 ——
# 之後任何真正的洩漏都可能剛好落在被放寬的範圍裡，而那正是這個檔案存在的理由。
OPERATOR_KEYS = {
    "who", "owner", "approved_by", "created_by", "updated_by",
    # 敏感路由清單的「誰加的／誰停的」。移除一條路由就是製造盲區，
    # 操作者必須看得見 —— 同 approved_by。
    "added_by", "removed_by",
    "email", "logout_url", "ros_url",
}

# 豁免掉的操作者欄位仍要另外斷言：必須是內部網域，不可以是消費者位址。
INTERNAL_DOMAIN = re.compile(r"@olis\.com\.tw$")

# **後台帳號本身就可能是一個 email。** backend 的 `acc` 是 Ocard 員工的登入帳號，
# 其中一部分是位址形式（實測 hetty@ocard.co / victor@ocard.co / jacky@ocard.co
# 出現在 R05 事件的母體排名裡）。政策明定帳號**原樣顯示** —— 那是這個工具存在的
# 目的，不是外流；上面第 2 條「該顯示的真的顯示」守的正是同一件事。
#
# 這條路徑用的是 `_scan_entity_panel()` 的結構性豁免：**只**放行對象標籤欄位、
# **只**放行內部網域，而且標籤仍然要過手機與憑證值的檢查。消費者的 gmail 出現在
# 標籤裡照樣失敗。這不是放寬 `EMAIL`，理由同 OPERATOR_KEYS 那段。
#
# `ocard.co` 與 `olis.com.tw` 分別是產品端與公司端的內部網域；消費者不會有這兩個
# 網域的位址（消費者的 Email 在 `params` 裡，由 `masking.scrub_text()` 清掉）。
ACCOUNT_DOMAIN = re.compile(r"@(?:olis\.com\.tw|ocard\.co)$")


def _strip_operator_fields(value):
    """遞迴移除「操作者是誰」的欄位，回傳 (清理後的結構, 被移除的值)。"""
    removed: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in OPERATOR_KEYS:
                    if isinstance(v, str) and "@" in v:
                        removed.append(v)
                    continue
                out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(value), removed


def _scan(payload: str, where: str) -> None:
    """不該外流的東西一律不得出現。"""
    assert not PHONE.search(payload), f"{where} 洩漏消費者手機號碼"
    for mail in EMAIL.findall(payload):
        assert mail in EMAIL_ALLOW, f"{where} 洩漏 Email {mail}"
    leak = CREDENTIAL_LEAK.search(payload)
    assert leak is None, f"{where} payload 內的憑證值未清洗：{leak.group(0)[:60] if leak else ''}"


def _pop_labels(value) -> tuple[list[str], object]:
    """遞迴抽出所有 `label` 欄位的字串值，回傳 (被抽出的值, 其餘結構)。"""
    labels: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "label" and isinstance(v, str):
                    labels.append(v)
                    continue
                out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return labels, walk(value)


def _scan_entity_panel(body, where: str) -> None:
    """對象面板專用：標籤裡的帳號可以是內部網域的 email，其餘一律最嚴格。

    母體排名列的是**其他**對象，而 backend 的對象就是帳號（見 ACCOUNT_DOMAIN）。
    豁免的範圍刻意只有「label 欄位 × 內部網域 × email」這一格：
    標籤仍要過手機與憑證值檢查，結構的其他部分仍走原本的 `_scan()`。
    """
    import json
    labels, cleaned = _pop_labels(body)
    _scan(json.dumps(cleaned, ensure_ascii=False), where)

    blob = " · ".join(labels)
    assert not PHONE.search(blob), f"{where} 的對象標籤洩漏消費者手機號碼"
    leak = CREDENTIAL_LEAK.search(blob)
    assert leak is None, f"{where} 的對象標籤含未清洗的憑證值"
    for mail in EMAIL.findall(blob):
        assert mail in EMAIL_ALLOW or ACCOUNT_DOMAIN.search(mail), (
            f"{where} 的對象標籤出現非內部網域的 Email {mail} —— "
            "「帳號原樣顯示」的政策只涵蓋內部帳號，消費者位址仍是外流")


def _scan_json(body, where: str) -> None:
    """給會回傳操作者 Email 的端點用：先結構性豁免，再逐項斷言。"""
    import json
    cleaned, operators = _strip_operator_fields(body)
    _scan(json.dumps(cleaned, ensure_ascii=False), where)
    for mail in operators:
        # 自由文字（例如有人把 email 打進「用途」欄）不在這裡 —— 那由上面的
        # _scan 擋。這裡只驗真正的操作者欄位。
        assert INTERNAL_DOMAIN.search(mail), \
            f"{where} 的操作者欄位出現非內部網域的位址 {mail}"


def test_overview_response_is_clean(client):
    r = client.get("/api/overview?minutes=60")
    assert r.status_code == 200
    _scan(r.text, "GET /api/overview")


def test_overview_widest_window_is_clean(client):
    r = client.get("/api/overview?minutes=10080")
    assert r.status_code == 200
    _scan(r.text, "GET /api/overview?minutes=10080")


def test_events_response_is_clean(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    _scan(r.text, "GET /api/events")
    for e in r.json()["events"]:
        detail = client.get(f"/api/events/{e['evt_no']}")
        _scan(detail.text, f"GET /api/events/{e['evt_no']}")


def test_event_entity_panels_are_clean(client):
    """對象面板會列出**其他**對象（母體排名、endpoint 的來源清單）。

    那是刻意的（本主控台就是要追究是哪個帳號、哪個來源），但也因此是一個新的
    外流面：帳號與 IP 原樣顯示，手機／消費者 Email／憑證值一個都不能出現。

    帳號本身是 email 形式時走 `_scan_entity_panel()` 的結構性豁免（只放行對象
    標籤欄位裡的內部網域位址）—— 實測 R05 的母體排名會列出 `hetty@ocard.co`
    這類員工帳號，那是政策要求顯示的值。
    """
    evts = [e["evt_no"] for e in client.get("/api/events").json()["events"]][:6]
    assert evts, "DB 裡沒有事件，這個測試會變成空跑"
    for evt in evts:
        for path in (f"/api/events/{evt}/entity",
                     f"/api/events/{evt}/entity/timeline?days=3"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:200]}"
            _scan_entity_panel(r.json(), f"GET {path}")


# --- 對象標籤豁免的反向測試（不需要 ClickHouse）--------------------------------

def test_entity_panel_exemption_still_rejects_a_consumer_email():
    """豁免只涵蓋內部網域。放寬 ACCOUNT_DOMAIN 必須在這裡失敗。

    沒有這條反向測試的話，有人為了讓某個端點變綠而多加一個網域，
    或乾脆改成 `@` 就放行，都不會有任何測試失敗 —— 而這個檔案存在的理由
    就是「之後任何真正的洩漏都可能剛好落在被放寬的範圍裡」。
    """
    import pytest
    ok = {"peers": {"top": [{"label": "1.34.41.218 · hetty@ocard.co"}]}}
    _scan_entity_panel(ok, "內部帳號")           # 不該拋

    leaked = {"peers": {"top": [{"label": "1.34.41.218 · someone@gmail.com"}]}}
    with pytest.raises(AssertionError, match="非內部網域"):
        _scan_entity_panel(leaked, "消費者位址")


def test_entity_panel_exemption_does_not_cover_phones_or_credentials():
    """標籤被抽出去單獨掃，但手機與憑證值的規則完全不變。"""
    import pytest
    with pytest.raises(AssertionError, match="手機"):
        _scan_entity_panel({"label": "0912345678"}, "標籤裡的手機")
    with pytest.raises(AssertionError, match="憑證"):
        _scan_entity_panel({"label": '"authorization": "Bearer abcdef123456"'}, "標籤裡的憑證")


def test_explorer_detail_is_clean(client):
    for source in ("api", "backend", "admin", "auth"):
        r = client.post("/api/explorer", json={
            "source": source, "analysis": "detail",
            "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 50})
        assert r.status_code == 200, r.text
        _scan(r.text, f"POST /api/explorer detail source={source}")


def test_explorer_detail_params_are_summarised_not_raw(client):
    """明細的 params 欄位只給大小與欄位名稱。完整原文走逐筆調閱端點。"""
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "detail",
        "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 20})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    for row in rows:
        params = row.get("params") or ""
        if params and params != "（空）":
            assert "bytes" in params, f"params 看起來是原文而非摘要：{params[:80]}"


def test_explorer_rankings_are_clean(client):
    for dim in ("endpoint", "brand", "source", "actor"):
        r = client.post("/api/explorer", json={
            "source": "backend", "analysis": dim,
            "start": "2026-07-16 00:00:00", "end": "2026-07-16 02:00:00"})
        assert r.status_code == 200, r.text
        _scan(r.text, f"POST /api/explorer {dim}")


def test_auth_actor_dimension_still_uses_token_fingerprint(client):
    """auth 的「操作者」維度是 token —— 那是有效憑證，必須維持指紋。"""
    r = client.post("/api/explorer", json={
        "source": "auth", "analysis": "actor",
        "start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"})
    assert r.status_code == 200, r.text
    names = [row["name"] for row in r.json()["rows"]]
    assert names, "這個區間沒有 auth 資料，測試等於沒驗到東西"
    for n in names:
        assert n.startswith("token_") or n == "（空）", f"token 未指紋化：{n!r}"


def test_quick_templates_are_clean(client):
    cases = [
        ("t01", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t03", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t06", {"start": "2026-07-16 00:00:00", "end": "2026-07-16 06:00:00"}),
        ("t12", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t13", {}),
    ]
    for tid, params in cases:
        r = client.post(f"/api/quick/{tid}", json=params)
        assert r.status_code == 200, f"{tid}: {r.text}"
        _scan(r.text, f"POST /api/quick/{tid}")


def test_sparklines_response_is_clean(client):
    r = client.get("/api/sparklines")
    assert r.status_code == 200
    _scan(r.text, "GET /api/sparklines")


def test_brands_response_is_clean(client):
    for q in ("", "a", "瓦城"):
        r = client.get(f"/api/brands?q={q}")
        assert r.status_code == 200
        _scan(r.text, f"GET /api/brands?q={q}")


def test_endpoints_response_is_clean(client):
    # 這個端點的 start/end 沒有預設值（空字串會被 FilterError 擋成 400），
    # 必須帶區間。
    window = {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}
    for source in ("api", "backend", "admin"):
        r = client.get("/api/endpoints", params={"source": source, **window})
        assert r.status_code == 200, r.text
        _scan(r.text, f"GET /api/endpoints?source={source}")


# ── 該顯示的真的顯示 ───────────────────────────────────────────────

def test_sweep_reveals_accounts_and_ips(client):
    """掃描清單必須給得出原始帳號與 IP，否則使用者無法追究問題。

    區間涵蓋 7/16 事件：那段期間 backend log 裡有 andrew_c 這個帳號，
    以及攻擊來源 131.143.215.229。
    """
    r = client.post("/api/sweep", json={"start": "2026-07-15", "end": "2026-07-18"})
    assert r.status_code == 200, r.text
    body = r.json()
    _scan(r.text, "POST /api/sweep")

    assert body["summary"]["findings"] > 0, "掃描沒有任何命中，這個測試等於沒驗到東西"
    entities = {f["entity"] for f in body["findings"]}
    assert not any(e.startswith(("actor_", "src_")) for e in entities), \
        f"對象仍是指紋形式：{sorted(entities)[:3]}"
    assert "andrew_c" in entities, \
        f"掃描沒給出帳號原始值（實際為 {sorted(entities)[:5]}）"

    # 每一列都要有讀得懂的一句話與逐項說明
    for f in body["findings"]:
        assert f["headline"], f"{f['entity']} 缺少 headline"
        assert f["explains"], f"{f['entity']} 缺少逐項說明"

    reread = client.get(f"/api/sweep/{body['sweep_no']}")
    assert reread.status_code == 200
    _scan(reread.text, f"GET /api/sweep/{body['sweep_no']}")
    assert reread.json()["findings"][0]["entity"] == body["findings"][0]["entity"]


def test_sweep_headline_names_the_brand(client):
    """「發生了什麼」必須點名品牌 —— 只給編號無法判斷影響對象。"""
    r = client.post("/api/sweep", json={"start": "2026-07-15", "end": "2026-07-18"})
    assert r.status_code == 200, r.text
    findings = r.json()["findings"]
    with_brand = [f for f in findings if (f["context"].get("brand_top") or [])]
    assert with_brand, "沒有任何一列帶出品牌，品牌對照可能壞了"
    for f in with_brand[:3]:
        top = f["context"]["brand_top"][0]["label"]
        # 「名稱（編號）」的格式；查不到名稱時 brands.py 會標明原因而非留空
        assert "（" in top and "）" in top, f"品牌標籤格式不對：{top!r}"


def test_explorer_detail_reveals_source_actor_brand_and_store(client):
    """Explorer 明細必須給得出「是誰、從哪來、影響哪個品牌與分店」。

    這條測的是**鍵名存在**，不只是值不像指紋 —— 前端讀錯鍵時每一列都會顯示「—」，
    而值檢查會因為 `row.get(key)` 回 None 而靜靜通過。實際發生過：把
    `actor_fp` 改名成 `actor` 之後前端沒跟上，明細的「帳號」欄整欄空白。
    """
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail",
        "start": "2026-07-16 00:00:00", "end": "2026-07-16 00:10:00", "limit": 20})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows, "這個區間沒有 backend 明細，測試等於沒驗到東西"

    REQUIRED = ("row_id", "source_ip", "actor", "brand_label", "store_label", "params")
    for key in REQUIRED:
        assert key in rows[0], f"明細缺少 {key} 欄位（前端會顯示空白）"

    # 至少要有一列真的帶出帳號與來源，否則等於沒驗到
    assert any(r_["actor"] for r_ in rows), "沒有任何一列帶出帳號"
    assert any(r_["source_ip"] for r_ in rows), "沒有任何一列帶出來源 IP"
    for row in rows:
        for key in ("source_ip", "actor", "resource"):
            value = row.get(key)
            if value:
                assert not str(value).startswith(("src_", "actor_", "resource_")), \
                    f"{key} 仍是指紋：{value!r}"


def test_explorer_detail_row_id_can_fetch_raw_payload(client):
    """明細的 row_id 必須真的能用來調閱原文 —— 否則「調閱原文」按鈕是死的。"""
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail",
        "start": "2026-07-16 00:00:00", "end": "2026-07-16 00:10:00", "limit": 5})
    assert r.status_code == 200, r.text
    row_id = r.json()["rows"][0]["row_id"]
    assert row_id, "明細沒給 row_id"

    got = client.post("/api/explorer/payload",
                      json={"source": "backend", "row_id": row_id})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["row_id"] == row_id
    assert body["fields"], "調閱回來沒有任何欄位"
    assert "稽核" in body["warning"], "調閱結果必須說明已留痕"


# ── 規則、Allowlist、操作稽核三個端點 ─────────────────────────────
#
# 這三個會回傳操作者 Email（audit_log.who、核准人、覆寫者），所以走 _scan_json
# 的結構性豁免。**不可以改成擴充 EMAIL_ALLOW** —— 見那個常數上方的說明。

def test_rules_response_is_clean(client):
    r = client.get("/api/rules")
    assert r.status_code == 200, r.text
    _scan_json(r.json(), "GET /api/rules")


def test_rule_detail_response_is_clean(client):
    """詳細頁會回完整 SQL —— 那裡只有欄位名與表名，不該有任何識別值。"""
    for rule_id in ("R01", "R03", "R08A", "R12"):
        r = client.get(f"/api/rules/{rule_id}")
        assert r.status_code == 200, r.text
        _scan_json(r.json(), f"GET /api/rules/{rule_id}")


def test_allowlist_response_is_clean(client):
    r = client.get("/api/allowlist")
    assert r.status_code == 200, r.text
    _scan_json(r.json(), "GET /api/allowlist")


def test_sensitive_routes_response_is_clean(client):
    """清單會回操作者 Email（added_by / removed_by，走結構性豁免）；
    reason 是人工自由文字，必須已遮罩。

    **這個測試必須靠豁免才能過，不能靠巧合。** 種子列的 `added_by='seed'`
    根本不是 email；而 `client` 預設的 `X-Dev-User` 是 `dev@olis.com.tw`，
    剛好已經在 `EMAIL_ALLOW` 裡 —— 兩者都會讓拿掉 `OPERATOR_KEYS` 裡的
    `added_by`/`removed_by` 之後這個測試仍然通過，等於守著一個不會失敗的
    斷言。這裡改用 `X-Dev-User` 覆寫成一個 `@olis.com.tw`（`INTERNAL_DOMAIN`
    要求的網域）、但**不在** `EMAIL_ALLOW` 裡的位址新增一條路由：拿掉豁免
    的話 `_scan()` 會在這個位址上失敗，加回去才會過。
    """
    from console.store import db as _db

    route = "zzz_masking_audit_test/route"
    # @olis.com.tw：INTERNAL_DOMAIN 要求的網域，但刻意不在 EMAIL_ALLOW 裡。
    operator = "masking-test-operator@olis.com.tw"
    try:
        r = client.post("/api/sensitive-routes",
                        json={"route": route, "reason": "masking 驗收測試"},
                        headers={"X-Dev-User": operator})
        assert r.status_code == 200, r.text

        r = client.get("/api/sensitive-routes")
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(row["added_by"] == operator for row in body["routes"]), (
            "新增的那一列沒有出現在清單裡，這個測試會變成空跑")
        _scan_json(body, "GET /api/sensitive-routes")
    finally:
        with _db.tx() as conn:
            conn.execute("DELETE FROM sensitive_routes WHERE route = ?", (route,))


def test_audit_response_is_clean(client):
    """audit_log.who 是操作者（豁免）；reason 是人工輸入（必須已遮罩）。"""
    r = client.get("/api/audit?limit=200")
    assert r.status_code == 200, r.text
    _scan_json(r.json(), "GET /api/audit")


def test_allowlist_shows_the_raw_ip_not_a_fingerprint(client):
    """反向守護：把它指紋化的話抑制永遠不會命中，而且完全沒有錯誤訊息。

    `rules/engine._allowlist_hit` 比對的是 entity 欄位的**原值**，
    所以 allowlist 存的必須也是原值。有人「順手」加回遮罩的症狀是
    「例外看起來建好了，事件照樣一直來」。
    """
    entries = client.get("/api/allowlist").json()["entries"]
    if not entries:
        import pytest
        pytest.skip("allowlist 是空的，這個測試等於沒驗到東西")
    for e in entries:
        assert not str(e["source_ip"]).startswith("src_"), \
            f"Allowlist 的來源被指紋化了：{e['source_ip']!r}"
    # 至少一筆是可解析的 IP 形狀
    import ipaddress
    assert any(_is_ip(ipaddress, e["source_ip"]) for e in entries), \
        "沒有任何一筆是有效的 IP —— 那些條目不會命中任何來源"


def _is_ip(ipaddress_mod, value) -> bool:
    try:
        ipaddress_mod.ip_address(str(value))
        return True
    except ValueError:
        return False
