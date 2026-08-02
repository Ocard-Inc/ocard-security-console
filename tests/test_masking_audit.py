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


def _scan(payload: str, where: str) -> None:
    """不該外流的東西一律不得出現。"""
    assert not PHONE.search(payload), f"{where} 洩漏消費者手機號碼"
    for mail in EMAIL.findall(payload):
        assert mail in EMAIL_ALLOW, f"{where} 洩漏 Email {mail}"
    leak = CREDENTIAL_LEAK.search(payload)
    assert leak is None, f"{where} payload 內的憑證值未清洗：{leak.group(0)[:60] if leak else ''}"


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
