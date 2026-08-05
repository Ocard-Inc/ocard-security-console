"""測試不可以對正式 Slack 頻道發訊息。

`.env` 的 `SLACK_WEBHOOK_URL` 指向正式頻道，而 `config.slack_webhook_url()` 有
`lru_cache` 又自己 `load_dotenv` —— `monkeypatch.setenv` 清不掉它。曾經發生的事：
`test_allowlist_write.py` 每跑一次就對正式頻道發一輪「新增／修改／停用 Allowlist
例外」（`203.0.113.55`、理由「驗收」、操作者 `dev@olis.com.tw（開發模式）`）。

那不只是吵。Allowlist 的 ops 訊息是**唯一一個當事人改不掉的偵測型控制**
（見 `api/allowlist_routes._notify_change`），把值班的人訓練成忽略那個頻道，
等於把控制拆掉，而畫面上一切正常。

護欄在 `conftest.slack_outbox`。這個檔案守的是「它真的攔在傳輸層」——
攔在 `send_ops_message` 之類的上層會讓訊息格式化不再被測試執行，
那裡的欄位名錯誤就只會在正式環境現形。
"""
from __future__ import annotations

import logging

import pytest
import requests

from console.alerting import notify
from console.core import config
from console.store import db


# conftest 的 slack_outbox 是 function 範圍的，import 時還沒生效 ——
# 所以這裡拿到的是真正的傳輸層實作，用來驗證它自己的分支（開關、佇列）。
_REAL_SEND = notify._send


@pytest.fixture(autouse=True)
def _explode_on_http(monkeypatch):
    """任何真的 HTTP 送出都必須在這裡炸掉，而不是靜靜地送出去。"""
    def _boom(*_a, **_k):
        raise AssertionError("測試對外送出了 Slack 訊息 —— 那是正式頻道")

    monkeypatch.setattr(requests, "post", _boom)


@pytest.fixture
def slack_env(monkeypatch):
    """設 `SLACK_ENABLED` 並清掉 `slack_setting()` 的 lru_cache。

    `load_dotenv` 不覆蓋既有環境變數，所以 `setenv` 確實蓋得過 `.env`；
    但 cache 必須自己清，否則測到的是第一次呼叫時的值（同 `ch_config()` 那個坑）。
    """
    def _set(value: str, base_url: str | None = None):
        monkeypatch.setenv("SLACK_ENABLED", value)
        if base_url is not None:
            monkeypatch.setenv("CONSOLE_BASE_URL", base_url)
        config.slack_setting.cache_clear()
        return config.slack_setting()

    yield _set
    config.slack_setting.cache_clear()


def test_ops_message_is_captured_not_sent(slack_outbox):
    notify.send_ops_message("測試用標題", "測試用內容")
    assert len(slack_outbox) == 1, "訊息沒有進 outbox：攔截點不在 _send"
    assert "測試用標題" in slack_outbox[0]


def test_event_notification_is_captured_not_sent(slack_outbox):
    """`dispatch` 與 `send_ops_message` 是兩條路徑，都必須走同一個攔截點。"""
    notify.dispatch([{"kind": "new", "event": _event()}])
    assert len(slack_outbox) == 1
    assert "EVT-TEST" in slack_outbox[0]


def test_formatting_still_runs_under_the_stub():
    """攔截點在傳輸層，所以格式化的錯誤照樣要冒出來（少一個欄位就是 KeyError）。"""
    broken = {k: v for k, v in _event().items() if k != "severity"}
    with pytest.raises(KeyError):
        notify.dispatch([{"kind": "new", "event": broken}])


# ─────────────────────── 總開關（.env 的 SLACK_ENABLED）───────────────────────

@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8600", "http://localhost:8600", ""])
def test_unset_defaults_to_off_on_a_dev_machine(slack_env, base_url):
    """沒設定 + 本機網址 → 關閉。`CONSOLE_BASE_URL` 空的也算本機（不知道自己
    在哪就別發）—— 測試環境正是這個狀態。"""
    assert slack_env("", base_url).enabled is False
    assert config.slack_enabled() is False


def test_unset_defaults_to_on_in_production(slack_env):
    """沒設定 + 對外網址 → 開啟。

    **這是刻意的，不是漏洞。** 固定預設 false 的話，Secret Manager 裡那份
    prod.env 漏掉一行就會讓正式環境靜靜不發任何告警，而畫面完全正常 ——
    而且那取決於「有沒有人記得改另一個檔案」。推導的方向相反：正式環境什麼
    都不用改就是開的，要關才需要動手，而關掉是看得見的。
    """
    setting = slack_env("", "https://ros.ocard.co/security")
    assert setting.enabled is True
    assert "CONSOLE_BASE_URL" in setting.reason


def test_explicit_setting_beats_the_derived_default(slack_env):
    """明確設定一律優先 —— 正式網址上也關得掉，本機也開得起來。"""
    assert slack_env("false", "https://ros.ocard.co/security").enabled is False
    assert slack_env("true", "http://127.0.0.1:8600").enabled is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_recognized_true_values(slack_env, value):
    assert slack_env(value).enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_recognized_false_values(slack_env, value):
    setting = slack_env(value)
    assert setting.enabled is False and setting.recognized is True


def test_typo_is_off_and_flagged(slack_env):
    """`ture` 不可以靜靜變成「開啟」。

    白名單的方向是刻意的：打錯 → 關閉，而關閉在畫面上與 log 裡都看得見
    （`summary()` 會把原始值講出來）。反過來（`!= "false"`）會把設定錯誤
    解讀成「要發」，那是在猜使用者的意圖。
    """
    setting = slack_env("ture", "https://ros.ocard.co/security")
    assert setting.enabled is False, "無法辨識的值不可以落進『對外網址 → 開啟』那條路"
    assert setting.recognized is False
    assert "ture" in notify.summary()["note"]


def test_disabled_switch_does_not_queue(slack_env):
    """關閉時**不可以**寫 slack_queue。

    那張表的語意是「送出失敗，待補送」。把刻意不發的訊息寫進去，等於在
    累積一顆延遲炸彈：哪天把開關打開，`_flush_queue` 會把本機跑過的每一次
    replay 與驗收訊息一次倒進值班頻道。
    """
    slack_env("false")
    before = db.one("SELECT count(*) AS n FROM slack_queue")["n"]
    _REAL_SEND("不該進佇列")
    assert db.one("SELECT count(*) AS n FROM slack_queue")["n"] == before


def test_summary_separates_the_two_reasons(slack_env, monkeypatch):
    """「開關沒開」與「沒有 webhook」的處置不同，note 不可以合併成「未啟用」。"""
    slack_env("false")
    assert "SLACK_ENABLED" in notify.summary()["note"]

    slack_env("true")
    monkeypatch.setattr(notify, "slack_webhook_url", lambda: "")
    summary = notify.summary()
    assert summary["enabled"] is False
    assert "SLACK_WEBHOOK_URL" in summary["note"]


def test_startup_logs_a_warning_when_disabled(slack_env, caplog):
    """停用必須是 WARNING。正式環境漏設 SLACK_ENABLED 的話，這一行與總覽的橫幅
    就是唯一的痕跡（選擇了「只警告、不擋啟動」）。"""
    slack_env("false")
    with caplog.at_level(logging.WARNING):
        notify.log_startup_status()
    assert any("Slack 通知已停用" in r.getMessage() for r in caplog.records)


def test_overview_banner_sees_the_switch(client, slack_env):
    """資安總覽的橫幅是「只警告不擋啟動」之下唯一看得見的痕跡。"""
    slack_env("false")
    sup = client.get("/api/overview").json()["suppression"]
    assert sup["slack"]["enabled"] is False
    assert sup["slack"]["note"]


def _event() -> dict:
    return {
        "evt_no": "EVT-TEST", "severity": "P1", "rule_name": "測試規則",
        "entity_label": "1.2.3.4", "metric_value": 100, "peak_value": 100,
        "threshold": 50, "baseline_median": 10, "multiple": 10.0,
        "first_seen": "2026-08-04 00:00:00", "last_seen": "2026-08-04 00:05:00",
        "brands": 0, "hit_count": 1, "context_json": None,
    }
