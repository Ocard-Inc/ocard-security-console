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


class ConfigError(RuntimeError):
    pass


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ConfigError(f"缺少必要環境變數 {key}（請確認 .env）")
    return val


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
