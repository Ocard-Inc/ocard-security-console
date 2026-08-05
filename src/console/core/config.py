"""全域設定載入：.env + config/settings.yaml，皆為唯讀 frozen 結構。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
STATE_DIR = PROJECT_ROOT / "state"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
WEB_DIR = PROJECT_ROOT / "web"


@dataclass(frozen=True)
class ChConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class SlackSetting:
    """`SLACK_ENABLED` 的解析結果。

    `raw` / `recognized` / `reason` 都是為了讓「不發通知」這件事說得出原因 ——
    打錯的值（`ture`）、明確關閉、以及依環境推導出來的關閉，在畫面上必須是
    三句不同的話，不然使用者無法知道要改哪裡。
    """
    enabled: bool
    raw: str
    recognized: bool
    reason: str


class ConfigError(RuntimeError):
    pass


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ConfigError(f"缺少必要環境變數 {key}（請確認 .env）")
    return val


@lru_cache(maxsize=1)
def _load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


# ─── 對外網址（隨環境而異，因此放 .env 而不是進版控的 settings.yaml）───
# 這兩個 getter 刻意不快取：值本身很便宜，快取只會讓測試與熱重載難以覆寫。

def ros_base_url() -> str:
    """Ocard ROS 的網址。留空 = 停用登入驗證（沒有任何保護）。"""
    _load_env()
    return os.environ.get("ROS_BASE_URL", "").strip().rstrip("/")


def console_base_url() -> str:
    """本主控台的對外網址，例如 https://ros.ocard.co/security。

    同時決定兩件事，因此只需設定這一個值、不會互相矛盾：
      1. Slack 告警連結的前綴
      2. 掛載路徑（未登入導回 ROS 時的 callbackUrl）
    """
    _load_env()
    return os.environ.get("CONSOLE_BASE_URL", "").strip().rstrip("/")


def console_mount_path() -> str:
    """從 CONSOLE_BASE_URL 推導出的掛載路徑（沒有子路徑時為空字串）。"""
    from urllib.parse import urlparse
    return urlparse(console_base_url()).path.rstrip("/")


@lru_cache(maxsize=1)
def ch_config() -> ChConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    return ChConfig(
        host=_require_env("CLICKHOUSE_HOST"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        user=_require_env("CLICKHOUSE_USER"),
        password=_require_env("CLICKHOUSE_PASSWORD"),
        database=os.environ.get("CLICKHOUSE_DB", "ocard"),
    )


@lru_cache(maxsize=1)
def mysql_config() -> MysqlConfig | None:
    """品牌名稱對照用的唯讀 MySQL（ocard.brand）。

    未設定 MYSQL_HOST 時回 None —— 品牌名稱只是輔助標示，缺它不該讓以
    ClickHouse 為核心的監測無法啟動；呼叫端會降級為僅顯示品牌編號。
    """
    load_dotenv(PROJECT_ROOT / ".env")
    host = os.environ.get("MYSQL_HOST", "").strip()
    if not host:
        return None
    return MysqlConfig(
        host=host,
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=_require_env("MYSQL_USER"),
        password=_require_env("MYSQL_PASSWORD"),
        database=os.environ.get("MYSQL_DB", "ocard"),
    )


@lru_cache(maxsize=1)
def fp_secret() -> bytes:
    load_dotenv(PROJECT_ROOT / ".env")
    return _require_env("FP_SECRET").encode("utf-8")


@lru_cache(maxsize=1)
def slack_webhook_url() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip()


_SLACK_TRUE = frozenset({"1", "true", "yes", "on"})
_SLACK_FALSE = frozenset({"0", "false", "no", "off"})
# 空字串 = 沒有 hostname 的網址，一律算本機（不知道自己在哪就別發）
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _looks_local(url: str) -> bool:
    """這個 process 是不是跑在開發機上，由**對外網址**判斷。

    `CONSOLE_BASE_URL` 沒設定時一律算本機：那個值在正式環境是必要的
    （決定掛載路徑與登入回跳），所以「空的」只會發生在開發機與測試裡。
    """
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


@lru_cache(maxsize=1)
def slack_setting() -> SlackSetting:
    """Slack 通知的總開關（`.env` 的 `SLACK_ENABLED`）。

    **明確設定一律優先**；沒設定時由 `CONSOLE_BASE_URL` 推導：指向 localhost
    （開發機）→ 關閉，指向對外網址（正式部署）→ 開啟。

    為什麼需要這個開關：開發機的 `.env` 帶的是**正式頻道**的 webhook，而本機會
    跑 pytest、replay、calibrate 與手動驗收。那些訊息送到值班頻道只會把人訓練成
    忽略它，而 Allowlist 的 ops 訊息是唯一一個當事人改不掉的偵測型控制。

    **為什麼預設值是推導而不是固定 `false`**：固定 false 的話，正式環境的
    `prod.env`（Secret Manager 裡的一份獨立檔案）漏了那一行就會**靜靜不發任何
    告警**，而主控台其餘部分完全正常 —— 那是這個系統最糟的失效模式，而且它
    取決於「有沒有人記得改另一個檔案」。推導的方向相反：正式環境什麼都不用改
    就是開的，要關才需要動手，而關掉這件事是看得見的（見下面兩個痕跡）。
    `CONSOLE_BASE_URL` 因此多了第四個用途（原本三個見 `console_base_url()`）。

    不發通知的痕跡有兩個，都不可以拿掉：`notify.log_startup_status()` 在啟動時
    記 WARNING、`notify.summary()` 讓資安總覽的「目前有部分監測被我們自己關閉」
    橫幅固定顯示，兩者都會說出**是哪一個原因**（`reason`）。

    **開啟值是白名單，不是 `!= "false"`。** 後者會讓 `SLACK_ENABLED=ture` 靜靜
    變成「開啟」，那是把設定錯誤解讀成意圖；白名單的方向相反 —— 打錯就是關閉，
    而關閉是看得見的（橫幅與啟動 log 會把原始值印出來）。
    """
    load_dotenv(PROJECT_ROOT / ".env")
    raw = os.environ.get("SLACK_ENABLED", "").strip()
    low = raw.lower()
    if low in _SLACK_TRUE:
        return SlackSetting(True, raw, True, "SLACK_ENABLED 明確開啟")
    if low in _SLACK_FALSE:
        return SlackSetting(False, raw, True, "SLACK_ENABLED 明確關閉")
    if low:
        return SlackSetting(
            False, raw, False,
            f"SLACK_ENABLED 的值「{raw}」無法辨識（可用 1／true／yes／on），視為關閉")
    if _looks_local(console_base_url()):
        return SlackSetting(
            False, raw, True,
            "未設定 SLACK_ENABLED，而 CONSOLE_BASE_URL 指向本機 —— "
            "開發環境預設不發")
    return SlackSetting(True, raw, True,
                        "未設定 SLACK_ENABLED，而 CONSOLE_BASE_URL 是對外網址")


def slack_enabled() -> bool:
    return slack_setting().enabled


@lru_cache(maxsize=1)
def settings() -> dict:
    """settings.yaml 全文（dict，呼叫端視為唯讀）。"""
    path = CONFIG_DIR / "settings.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 格式錯誤：頂層必須是 mapping")
    return data


def source_table(source_key: str) -> str:
    """UI 資料來源代碼 → ClickHouse 表名（白名單）。"""
    sources = settings()["data_sources"]
    if source_key not in sources:
        raise ConfigError(f"未知資料來源 {source_key!r}，允許：{sorted(sources)}")
    return sources[source_key]["table"]
