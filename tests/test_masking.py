"""識別值呈現政策的單元測試。

政策在 `core/masking.py` 的模組說明裡：**調查對象（帳號、IP、訂單號、會員 ID）
原樣顯示；有效憑證（token）與 payload 原文仍收斂。**

這個檔案兩邊都測：
- 該顯示的要真的顯示（避免有人「順手」加回遮罩，讓工具再次無法追究問題）
- 該收斂的要真的收斂（token 是還能用的憑證；payload 裡混著消費者手機與 API secret）

CLICKHOUSE_* 由 conftest 從 .env 載入；此處不可塞假值，
否則 lru_cache 會讓同一 session 後續的真實查詢全部連到假主機。
"""
from console.core import masking


# ── 調查對象：原樣顯示 ─────────────────────────────────────────────

def test_account_ip_and_resource_are_returned_verbatim():
    """這是功能需求，不只是「沒有遮罩」——查不到對象的資安主控台沒有用。"""
    assert masking.actor("andrew_c") == "andrew_c"
    assert masking.src("131.143.215.229") == "131.143.215.229"
    assert masking.resource("ORD-12345") == "ORD-12345"


def test_multi_hop_xff_string_kept_whole():
    """多段 XFF 是判定偽造來源的依據，不可切斷或改寫。"""
    raw = "127.0.0.1, 13.125.88.63"
    assert masking.src(raw) == raw


def test_empty_values_normalise_to_none():
    """空值一律 None，呼叫端才能區分「沒有」與「空字串」。"""
    for fn in (masking.actor, masking.src, masking.resource):
        assert fn("") is None
        assert fn(None) is None
        assert fn("None") is None
        assert fn("null") is None


def test_surrounding_whitespace_stripped():
    assert masking.src("  1.2.3.4  ") == "1.2.3.4"


def test_actor_caps_length_for_attacker_controlled_values():
    """`masking.actor()` 的值不再保證來自一個有長度限制的資料庫欄位。

    R07A 從 params 取回新版登入端點不寫的 acc（見
    config/rules/r07a_login_failed_acc.yaml），那是攻擊者可以自由填寫的登入
    表單欄位，沒有經過任何長度驗證就落地。這個值會變成事件去重鍵
    （entity_key）的一部分，也會原樣進 Slack 訊息文字——沒有上限的話，一個
    刻意塞超長字串的登入請求可以撐大這兩者。真實帳號名遠遠不會碰到這個上限。
    """
    huge = "a" * 5000
    out = masking.actor(huge)
    assert len(out) < len(huge)
    assert out.startswith("a" * 200)
    assert "5000" in out, "截斷說明要帶原長，不能只是靜靜切掉"
    # 正常長度的帳號名完全不受影響
    assert masking.actor("andrew_c") == "andrew_c"


# ── token：仍然是不可逆指紋 ────────────────────────────────────────

def test_token_is_still_fingerprinted():
    """token 是**還有效的憑證**。顯示它等於任何有主控台讀取權的人都能冒用該身分。"""
    raw = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    fp = masking.token_fp(raw)
    assert fp.startswith("token_") and len(fp) == len("token_") + 12
    assert raw not in fp
    assert "payload" not in fp


def test_token_fingerprint_is_deterministic():
    """同一個 token 永遠得到同一個指紋 —— 它還要能當關聯鍵與去重鍵。"""
    a = masking.token_fp("abc")
    b = masking.token_fp("abc")
    assert a == b and a != masking.token_fp("abd")


def test_token_empty_returns_none():
    assert masking.token_fp("") is None
    assert masking.token_fp(None) is None


def test_display_funcs_covers_every_entity_kind():
    """`config/rules/*.yaml` 的 `entity[].fp` 與探針的 `fp_kind` 都從這裡查函式。
    少一個鍵會在執行期 KeyError，不是靜靜失效。"""
    assert set(masking.DISPLAY_FUNCS) == {"actor", "src", "resource", "token"}
    assert masking.DISPLAY_FUNCS["token"] is masking.token_fp


# ── payload：預設收斂 ──────────────────────────────────────────────

def test_scrub_text_masks_credentials():
    raw = '{"token": "abc123secretvalue", "user": "x", "Authorization": "Bearer zzz"}'
    out = masking.scrub_text(raw)
    assert "abc123secretvalue" not in out
    assert "Bearer zzz" not in out
    assert "***" in out


def test_scrub_text_masks_consumer_pii():
    """手機與 Email 是**消費者**個資，不是調查對象。

    這些欄位的去向不只畫面：notify.py 會把事件內容送進 Slack，
    應用 log 明文寫在 state/logs/*.log。
    """
    out = masking.scrub_text("cell=0912345678 mail=someone@example.com")
    assert "0912345678" not in out
    assert "someone@example.com" not in out


def test_scrub_text_truncates():
    out = masking.scrub_text("x" * 1000, max_len=100)
    assert len(out) < 200 and "截斷" in out


def test_payload_summary_gives_field_names_but_no_values():
    raw = '{"phone": "0911222333", "order_no": "A9987"}'
    out = masking.payload_summary(raw)
    assert "0911222333" not in out
    assert "A9987" not in out
    assert "phone" in out and "order_no" in out    # 欄位名可以，值不行
    assert "bytes" in out                          # 大小也給，方便判斷有沒有內容


def test_payload_summary_handles_empty():
    assert masking.payload_summary(None) == "（空）"
    assert masking.payload_summary("") == "（空）"
