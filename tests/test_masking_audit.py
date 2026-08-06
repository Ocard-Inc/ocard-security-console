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

from console.core import admins, masking

# 消費者個資樣式：台灣手機、Email
PHONE = re.compile(r"\b09\d{8}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# 操作者自己的 email 是刻意留痕的（audit_log.who、sweeps.created_by、session 端點），
# 不屬於消費者個資。dev@ 是離線模式下 X-Dev-User 的預設值。
EMAIL_ALLOW = {"vinek@olis.com.tw", "dev@olis.com.tw"}

# payload 裡出現這些字樣代表憑證值沒有被清洗掉。
# 值本身是隨機的，所以檢查的是「鍵後面直接跟著一段非 *** 的值」。
CREDENTIAL_LEAK = re.compile(
    r'(?i)"(?:authorization|auth|cookie|password|pwd|secret|api[_-]?key|vtoken)"\s*:\s*"(?!\*\*\*)[^"]{6,}"'
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
# 這條路徑用的是 `_scan_entity_panel()` 的結構性豁免：**只**放行對象標籤欄位，
# 而且標籤仍然要過手機與憑證值的檢查。這不是放寬 `EMAIL`，理由同 OPERATOR_KEYS 那段。
#
# `ocard.co` 與 `olis.com.tw` 分別是產品端與公司端的內部網域；消費者不會有這兩個
# 網域的位址（消費者的 Email 在 `params` 裡，由 `masking.scrub_text()` 清掉）。
ACCOUNT_DOMAIN = re.compile(r"@(?:olis\.com\.tw|ocard\.co)$")

# **商家的後台帳號就是他自己的 gmail。** 內部網域那條只涵蓋 Ocard 員工
# （`hetty@ocard.co`），而 `ods_admin_log.acc` 有大量商家自己註冊的位址
# —— 實測 EVT-0034（R07A、source=admin、對象維度是 actor）的母體排名第 10 名是
# `a092011100@gmail.com`。政策明定**後台帳號原樣顯示**，那是這個工具唯一的用途。
#
# 所以豁免改成掛在**維度**上而不是掛在網域上：這一格顯示的是 `actor`
# （帳號欄位本身），裡面的 email 形式就是帳號，不是消費者位址。
# 逐筆把帳號加進 `EMAIL_ALLOW` 是另一條路，但那會讓這個測試每來一個新商家就紅一次，
# 而「加一行就好」的習慣會把這個檔案的意義掏空。
#
# 這個豁免**放棄**了一種偵測：消費者 Email 出現在**帳號標籤**裡不會再被抓到。
# 換來的邊界是明確的 —— 其餘每一個維度（endpoint／brand／store／source_ip）
# 的標籤仍然嚴格，手機與憑證值在帳號標籤裡也照樣失敗。
# `auth`／`api` 的 actor 是不可逆的 token 指紋，不會是 email 形式。
ACTOR_FIELDS = {"actor"}


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


def _pop_labels(value) -> tuple[list[str], list[str], object]:
    """抽出對象標籤，回傳 (帳號維度的標籤, 其餘標籤, 剩下的結構)。

    「這個標籤是不是帳號」由**結構**決定，不是由字串長相決定：

    - `/entity` 的頂層有 `dims: [{field, label, value}]`，那是這個事件的對象
      維度。其中有 `actor` 時，`peers.top[].label`（同一組維度算出來的其他對象）
      與 `keys[]`（回送用的原始值）就是帳號。
      **只有 `peers` 繼承這個範圍**：同一份回應裡的 `share.rows[].label` 是
      **來源 IP** 清單、`profile` 是時段分布，那些不是帳號欄位，
      整包一起豁免等於把 email 檢查從半個回應上拿掉。
    - `/entity/breakdown` 的 `dims[]` 每一格自己帶 `field`，`field == 'actor'`
      的那一格，它的 `rows[].label` 是帳號。

    複合維度（例如 actor + endpoint）的 label 是一整串 `帳號 · 端點`，
    沒辦法只豁免前半段 —— 但 endpoint 不會是 email 形式，代價可接受。
    """
    accounts: list[str] = []
    others: list[str] = []

    def take(node, bucket):
        """把這棵子樹裡的 label / keys 全部收進指定的桶。"""
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ("label", "value") and isinstance(v, str):
                    bucket.append(v)
                    continue
                if k == "keys" and isinstance(v, list):
                    bucket.extend(x for x in v if isinstance(x, str))
                    continue
                out[k] = take(v, bucket)
            return out
        if isinstance(node, list):
            return [take(v, bucket) for v in node]
        return node

    def walk(node, actor_scope=False):
        if isinstance(node, dict):
            # breakdown 的一格：自己宣告了 field，由它決定這一格算不算帳號
            field = node.get("field")
            if isinstance(field, str):
                return take(node, accounts if field in ACTOR_FIELDS else others)
            out = {}
            for k, v in node.items():
                if k in ("label", "value") and isinstance(v, str):
                    (accounts if actor_scope else others).append(v)
                    continue
                if k == "keys" and isinstance(v, list):
                    (accounts if actor_scope else others).extend(
                        x for x in v if isinstance(x, str))
                    continue
                # **只有母體排名進入帳號範圍**（白名單，不是排除法）。同一份回應裡
                # 的 `share` 是來源 IP 清單、`profile` 是時段分布 —— 一起豁免等於把
                # email 檢查從半個回應上拿掉，而那不是這個豁免要換的東西。
                # 用白名單是因為新增區塊時預設要落在「嚴格」那一邊。
                out[k] = walk(v, actor_scope or (in_actor and k == "peers"))
            return out
        if isinstance(node, list):
            return [walk(v, actor_scope) for v in node]
        return node

    dims = value.get("dims") if isinstance(value, dict) else None
    in_actor = bool(dims) and any(
        isinstance(d, dict) and d.get("field") in ACTOR_FIELDS for d in dims)
    return accounts, others, walk(value, False)


def _scan_entity_panel(body, where: str) -> None:
    """對象面板專用：帳號維度的標籤可以是任何 email 形式，其餘一律最嚴格。

    母體排名列的是**其他**對象，而 admin／backend 的對象就是帳號
    （見 ACTOR_FIELDS 那段：Ocard 員工是內部網域，商家是自己的 gmail）。
    豁免的範圍是「**`actor` 維度的**標籤 × email」這一格：
    標籤仍要過手機與憑證值檢查，其他維度的標籤仍只放行內部網域，
    結構的其他部分仍走原本的 `_scan()`。
    """
    import json
    accounts, others, cleaned = _pop_labels(body)
    _scan(json.dumps(cleaned, ensure_ascii=False), where)

    # 帳號標籤：email 不限網域（帳號本來就是它），其餘規則完全不變
    acct = " · ".join(accounts)
    assert not PHONE.search(acct), f"{where} 的帳號標籤洩漏消費者手機號碼"
    assert CREDENTIAL_LEAK.search(acct) is None, f"{where} 的帳號標籤含未清洗的憑證值"

    blob = " · ".join(others)
    assert not PHONE.search(blob), f"{where} 的對象標籤洩漏消費者手機號碼"
    leak = CREDENTIAL_LEAK.search(blob)
    assert leak is None, f"{where} 的對象標籤含未清洗的憑證值"
    for mail in EMAIL.findall(blob):
        assert mail in EMAIL_ALLOW or ACCOUNT_DOMAIN.search(mail), (
            f"{where} 的對象標籤出現非內部網域的 Email {mail} —— "
            "「帳號原樣顯示」的政策只涵蓋帳號維度（actor），"
            "其他維度出現消費者位址仍是外流")


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


# `account`（Explorer 的 ranking()／detail()，2026-08 加）是 `ods_user_admin.acc`
# 對照出來的**後台帳號名**，依 `core/masking.py` 的政策原樣顯示 —— 同
# OPERATOR_KEYS 那段的道理，但形狀不同，不能直接塞進那個字典：
#
# 實測 Order Log 的 POS 整合帳號會用電話號碼命名（`idx=3731` → `0900480856`，
# 命中 `\b09\d{8}\b`）、也會是外部 Email（`idx=43137` →
# `f10205071020507@gmail.com`，不在任何內部網域）。兩者都會誤觸 PHONE／EMAIL
# 樣式產生假警報「Explorer 明細洩漏消費者手機號碼／Email」——而 `account` 從來
# 不是消費者資料，是後台整合帳號的名字。
#
# `INTERNAL_DOMAIN`／`ACCOUNT_DOMAIN` 這兩個網域檢查在這裡都不適用（帳號名可以
# 是任意網域甚至沒有網域），所以斷言**形狀**沒有意義。改成斷言**出處**：
# 移除的 `account` 值必須等於 `admins.account()` 對同一列 `actor`（`detail()`）
# 或 `name`（`ranking()`）算出來的結果 —— 直接證明它來自 `ods_user_admin`
# 對照，不是從 `params`／`headers` 漏出來的東西。一旦有人把別的字串塞進這個欄位，
# 這個斷言就會失敗，而樣式檢查（放寬 PHONE／EMAIL）永遠不會抓到這種置換。
def _strip_account_fields(value):
    """遞迴移除 `account` 欄位，回傳 (清理後的結構, [(anchor, account值), ...])。

    `anchor` 是同一列的 `actor`（明細）或 `name`（排名）—— 那兩個鍵在 `ranking()`／
    `detail()` 的輸出裡本來就是原始的 `_admin` 整數字串，`admins.account()` 拿它
    就能重算出同一個帳號名。
    """
    pairs: list[tuple[object, str]] = []

    def walk(node):
        if isinstance(node, dict):
            account = node.get("account")
            if isinstance(account, str) and account:
                pairs.append((node.get("actor", node.get("name")), account))
            out = {}
            for k, v in node.items():
                if k == "account":
                    continue
                out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(value), pairs


def _scan_explorer(body, where: str) -> None:
    """給會回傳 `account`（後台帳號對照）的 Explorer 端點用。

    豁免的是「這個鍵的值不受 PHONE／EMAIL 樣式檢查」，換來的保證是
    「它必須等於 `admins.account(anchor)` 的結果」—— 見上方常數區塊的說明。
    """
    import json
    cleaned, pairs = _strip_account_fields(body)
    _scan(json.dumps(cleaned, ensure_ascii=False), where)
    for anchor, account in pairs:
        assert account == admins.account(anchor), (
            f"{where} 的 account 欄位（{account!r}，anchor={anchor!r}）與帳號對照"
            f"結果不符 —— 可能不是真的來自 ods_user_admin 對照")


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
        # 拆解與趨勢是 2026-08 新增的**新外流面**：`breakdown` 逐維度列出
        # 這個對象打了哪些 endpoint／品牌／分店／帳號，那是四份新的對象清單。
        # 漏掉的話新面板可以外流而整個檔案照樣全綠。
        for path in (f"/api/events/{evt}/entity",
                     f"/api/events/{evt}/entity/breakdown",
                     f"/api/events/{evt}/entity/trend?minutes=180",
                     f"/api/events/{evt}/entity/timeline?days=3"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:200]}"
            _scan_entity_panel(r.json(), f"GET {path}")


# --- 對象標籤豁免的反向測試（不需要 ClickHouse）--------------------------------

def test_entity_panel_exemption_still_rejects_a_consumer_email():
    """沒有帳號維度時，豁免只涵蓋內部網域。放寬 ACCOUNT_DOMAIN 必須在這裡失敗。

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


def test_account_exemption_is_keyed_on_the_dimension_not_on_the_string():
    """同一個 gmail：在 `actor` 維度是帳號（放行），在別的維度是外流（失敗）。

    這是「A 案」的核心 —— 豁免掛在**這一格顯示的是什麼欄位**上，
    不是掛在值長什麼樣。把 `ACTOR_FIELDS` 加進 `source_ip`／`endpoint`
    之類的欄位，或改成不看 `field` 一律放行，都必須在這裡失敗。
    """
    import pytest
    mail = "a092011100@gmail.com"

    # ① 事件的對象維度是 actor → 母體排名列的是別的帳號，放行
    ok = {"dims": [{"field": "actor", "label": "操作者", "value": "vibesktv"}],
          "peers": {"top": [{"label": mail, "keys": [mail]}]}}
    _scan_entity_panel(ok, "帳號維度")

    # ② 同一份結構，維度換成來源 IP → 那一格不該出現 email
    leaked = {"dims": [{"field": "source_ip", "label": "來源 IP", "value": "1.2.3.4"}],
              "peers": {"top": [{"label": mail, "keys": [mail]}]}}
    with pytest.raises(AssertionError, match="非內部網域"):
        _scan_entity_panel(leaked, "IP 維度")

    # ③ **同一份回應裡的其他區塊不繼承。** `share` 列的是來源 IP，
    #    帳號維度的豁免不可以順手把那半個回應的 email 檢查也關掉。
    spread = {"dims": [{"field": "actor", "label": "操作者", "value": "vibesktv"}],
              "share": {"rows": [{"label": mail, "count": 3}]}}
    with pytest.raises(AssertionError, match="非內部網域"):
        _scan_entity_panel(spread, "帳號維度不可外溢到來源清單")

    # ④ breakdown 逐格自己宣告 field：actor 那格放行、brand 那格不放行
    _scan_entity_panel(
        {"dims": [{"field": "actor", "label": "操作者",
                   "rows": [{"label": mail, "count": 3}]}]}, "拆解的帳號那格")
    with pytest.raises(AssertionError, match="非內部網域"):
        _scan_entity_panel(
            {"dims": [{"field": "brand", "label": "品牌",
                       "rows": [{"label": mail, "count": 3}]}]}, "拆解的品牌那格")


def test_entity_panel_exemption_does_not_cover_phones_or_credentials():
    """標籤被抽出去單獨掃，但手機與憑證值的規則完全不變 —— 帳號維度也一樣。"""
    import pytest
    with pytest.raises(AssertionError, match="手機"):
        _scan_entity_panel({"label": "0912345678"}, "標籤裡的手機")
    with pytest.raises(AssertionError, match="憑證"):
        _scan_entity_panel({"label": '"authorization": "Bearer abcdef123456"'}, "標籤裡的憑證")
    # 帳號維度只豁免 email，手機與憑證值照樣要炸
    with pytest.raises(AssertionError, match="帳號標籤洩漏消費者手機"):
        _scan_entity_panel(
            {"dims": [{"field": "actor", "label": "操作者", "value": "a"}],
             "peers": {"top": [{"label": "0912345678"}]}}, "帳號維度裡的手機")
    with pytest.raises(AssertionError, match="帳號標籤含未清洗的憑證值"):
        _scan_entity_panel(
            {"dims": [{"field": "actor", "label": "操作者", "value": "a"}],
             "peers": {"top": [{"label": '"authorization": "Bearer abcdef123456"'}]}},
            "帳號維度裡的憑證")


def test_explorer_detail_is_clean(client):
    for source in ("api", "backend", "admin", "auth", "order"):
        r = client.post("/api/explorer", json={
            "source": source, "analysis": "detail",
            "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 50})
        assert r.status_code == 200, r.text
        _scan_explorer(r.json(), f"POST /api/explorer detail source={source}")


def test_explorer_detail_account_field_survives_a_wider_window(client):
    """回歸測試：`account` 欄位裡的電話樣式帳號名不是假警報的來源。

    2026-08-06 實測：`source=order`、視窗 2026-08-01 12:00~12:10、`limit=5000`
    時，`actor=3731` 的帳號名是 `0900480856`（POS 整合帳號以電話號碼命名），
    命中 PHONE 樣式 `\\b09\\d{8}\\b`。上面那條測試的 `limit=50` 從未掃到這一列
    ——換一個視窗或調高 limit 就會產生假警報「Explorer 明細洩漏消費者手機號碼」。

    這裡直接用會踩到它的參數送出去，並先斷言真的掃到目標列（不能自己也跟著
    避開它），才呼叫 `_scan_explorer()`：這樣豁免與其驗證才算被真正驗證過，
    不是恰好沒被測到。
    """
    r = client.post("/api/explorer", json={
        "source": "order", "analysis": "detail",
        "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 5000})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    hit = [row for row in rows if row.get("account") == "0900480856"]
    assert hit, ("這個視窗應該要能掃到 actor=3731 的帳號 0900480856，"
                 "測試沒有踩到目標列，等於沒驗到東西")
    assert hit[0]["actor"] == "3731"
    assert PHONE.search(hit[0]["account"]), "目標值本身應該命中 PHONE 樣式，否則這條測試在測別的東西"
    _scan_explorer(r.json(), "POST /api/explorer detail source=order（含電話樣式帳號名）")


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
        # backend 的 actor 排名不會帶出 `account`（見 NUMERIC_ACTOR_SOURCES），
        # 但走 `_scan_explorer()` 而非 `_scan()` 是防禦性的一致做法 ——
        # 之後有人把 api／order 加進這個迴圈，不必記得換一個掃描函式。
        _scan_explorer(r.json(), f"POST /api/explorer {dim}")


def test_explorer_actor_ranking_account_field_survives_a_wider_window(client):
    """同一個風險的排名版本：`ranking()` 的 `account` 也可能是電話樣式帳號名。

    `ranking()` 的 anchor 是 `name`（原始 `_admin` 整數字串），不是 `actor`——
    這裡直接驗證 `_scan_explorer()` 對這個形狀也抓得到出處。
    """
    r = client.post("/api/explorer", json={
        "source": "order", "analysis": "actor",
        "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 500})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    accounts = [row["account"] for row in rows if row.get("account")]
    assert accounts, "這個視窗的 actor 排名應該要有帳號名，測試沒驗到東西"
    _scan_explorer(r.json(), "POST /api/explorer actor ranking source=order")


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
    for source in ("api", "backend", "admin", "order"):
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


# ── scrub_text 的憑證鍵清單 ────────────────────────────────────────────

def test_scrub_text_masks_the_auth_key():
    """Order Log 的 params 帶明文 "auth" session 憑證（POS／oboss）。

    這個值會流進規則 context → Slack 與 state/logs/*.log。漏掉的症狀是
    值班頻道上出現一個還有效的憑證，而畫面上一切正常。
    """
    out = masking.scrub_text(
        '{"_store":"4864","auth":"rzkAokVhOoLKV2fvHh53","lang":"zh-Hant",'
        '"platform":"oboss","uid":"7097"}')
    assert "rzkAokVhOoLKV2fvHh53" not in out, f"auth 憑證沒有被遮罩：{out}"
    # 不可以連無關的值一起吃掉 —— 那些是調查需要的資料
    assert "4864" in out
    assert "zh-Hant" in out
    assert "7097" in out


def test_scrub_text_still_masks_authorization():
    """加 auth 之後 authorization 不可以回歸。

    alternation 的順序不影響結果（`auth` 先匹配時，後面的 `\"?\\s*[:=]`
    比對 `orization` 會失敗而回溯到完整的 `authorization` 分支），但這件事
    必須有測試守著，否則下一個人重排順序時沒有任何訊號。
    """
    out = masking.scrub_text('{"authorization": "Bearer abcdef123456"}')
    assert "abcdef123456" not in out, f"authorization 沒有被遮罩：{out}"


def test_scrub_text_respects_prefix_boundaries():
    """regex 的前綴方向安全 —— `author` 與 `authority` 不會被遮罩。

    樣式開頭的 `\"?` 沒有錨定，但 `[:=]` 會卡在前綴詞的中間，所以不會誤傷。
    這是全部 9 個 alternation 共有的既有行為，不是 `auth` 新引入的，
    不要為了「精確」而加錨定（那會改全部 9 個鍵的行為，超出本 task 範圍）。
    """
    # author 與 authority 不應該被遮
    out_author = masking.scrub_text('{"author":"vinek","authority":"tw"}')
    assert '"author":"vinek"' in out_author, f"author 被誤遮：{out_author}"
    assert '"authority":"tw"' in out_author, f"authority 被誤遮：{out_author}"


def test_scrub_text_accepts_suffix_matching():
    """regex 的後綴方向會命中，這是刻意接受的既有行為。

    開頭 `\"?` 沒有錨定意味著任何以 key 結尾的鍵都會被遮罩。這不是 `auth`
    新引入的性質 —— `mytoken`、`mysecret` 等也一樣會被遮（既有行為）。

    理由：過度遮罩比洩漏安全。`oauth` 或 `is_auth` 被遮成 `***` 是無害噪音，
    但它們實際上是憑證，外流才是真的風險。布林旗標被誤遮只是信號有點混亂。
    """
    # oauth 與 is_auth 都應該被遮（後綴匹配）
    out_oauth = masking.scrub_text('{"oauth":"secret_value_123456"}')
    assert "secret_value_123456" not in out_oauth, f"oauth 沒有被遮罩：{out_oauth}"
    assert '{"oauth":***}' in out_oauth, f"oauth 的值未正確遮罩：{out_oauth}"

    out_is_auth = masking.scrub_text('{"is_auth":"token_value_789012"}')
    assert "token_value_789012" not in out_is_auth, f"is_auth 沒有被遮罩：{out_is_auth}"
    assert '{"is_auth":***}' in out_is_auth, f"is_auth 的值未正確遮罩：{out_is_auth}"

    # mytoken 也會被遮（既有行為，不是新的）
    out_mytoken = masking.scrub_text('{"mytoken":"abcdef123456"}')
    assert "abcdef123456" not in out_mytoken, f"mytoken 沒有被遮罩：{out_mytoken}"
# --- 回送閘門（`masking.echoable`）------------------------------------------

def test_echoable_says_yes_only_when_display_equals_the_raw_value():
    """點擊母體排名要把那一列的原始值回送後端，而回送的閘門是「呈現 == 原值」。

    **刻意用執行期比對，不是靜態的「哪些 kind 是單向的」清單**：`actor` 是否
    單向取決於帳號長度（超長會 HMAC 截斷），清單一定會漂移，而漂移的方向是
    靜靜地把指紋當原值送出去。
    """
    # mask 為 None 的維度（endpoint、品牌編號、分店編號）本來就是原樣顯示
    assert masking.echoable(None, "Api2/GetProfile") is True
    assert masking.echoable(None, "1180") is True

    # IP 與一般長度的帳號名：2026-08 的政策是原樣顯示，所以可以回送
    assert masking.echoable("src", "203.0.113.55") is True
    assert masking.echoable("actor", "andrew_c") is True

    # API token 永遠是指紋 —— 這是「還有效的憑證」，絕不可回送
    assert masking.echoable("token", "abcdef0123456789") is False

    # 超長帳號名會被截斷 + 附 HMAC 摘要，也是不可逆的
    assert masking.echoable("actor", "a" * 300) is False

    # 未知的 kind 一律當成不可回送（要炸就往安全的方向倒）
    assert masking.echoable("不存在的種類", "x") is False


def test_peer_keys_never_carry_a_credential(client):
    """`peers.top[].keys` 是回送用的原始值 —— 裡面不可以有憑證。

    `keys` 存在就等於「這個值的呈現等於它本身」（`masking.echoable()` 的閘門），
    所以逐段串起來必須等於 label。對不上代表有人把閘門拆掉了，而症狀是
    **主控台把不可逆的指紋還原成原始 token**，畫面上完全正常。

    只在維度全部原樣顯示時才比對 label —— 品牌與分店的 label 是「名稱（編號）」
    而 keys 是裸編號，那個差異是刻意的（見 test_event_entity 的
    `test_peer_keys_are_the_raw_values_not_the_named_labels`）。
    """
    named_kinds = {"brand", "store"}
    checked = 0
    for e in client.get("/api/events").json()["events"][:8]:
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if not p.get("supported"):
            continue
        fields = [d["field"] for d in p["dims"]]
        for row in p["peers"]["top"]:
            assert "keys" in row, f"{e['evt_no']} 少了 keys 鍵"
            if row["keys"] is None:
                continue
            checked += 1
            for v in row["keys"]:
                assert not v.startswith("token_"), \
                    f"{e['evt_no']} 的 keys 裡出現了 token 指紋：{v}"
            if not (named_kinds & set(fields)):
                assert " · ".join(row["keys"]) == row["label"], (
                    f"{e['evt_no']} 的 keys 與 label 不一致 —— "
                    f"echoable() 的閘門可能被拆掉了")
    assert checked, "沒有任何一列有 keys，這條測試等於沒有執行"
