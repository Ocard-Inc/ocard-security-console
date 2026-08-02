"""ClickHouse 唯讀查詢層。

修正 ocard-analyst utils/ch.py 的缺陷：
- thread-local client（原版每次呼叫新建且不關閉，高頻輪詢會洩漏 socket；
  但單一全域 client 也不行 —— clickhouse-connect 的 session 不允許並行查詢，
  FastAPI 的 threadpool 併發會撞上 "concurrent queries within the same session"）
- connect / send_receive / max_execution_time 三層 timeout
- SELECT-only 程式層守衛（readonly 無法作為 per-query setting 傳遞，
  伺服器將其標為唯讀設定；正式環境仍建議申請 readonly 專用帳號）
- 連線層錯誤自動重建 client 重試一次，並拋出可分類的自訂例外
"""
from __future__ import annotations

import logging
import threading

import clickhouse_connect
import pandas as pd
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from console.core.config import ch_config

logger = logging.getLogger(__name__)

_local = threading.local()

_QUERY_SETTINGS = {"max_execution_time": 55}

_ALLOWED_PREFIXES = ("select", "with", "describe", "show", "explain")


class ChQueryError(RuntimeError):
    """查詢執行失敗（SQL 錯誤或伺服器拒絕）。"""


class ChConnectionError(RuntimeError):
    """連線層失敗（daemon 據此觸發「監測中斷」告警）。"""


def _new_client():
    cfg = ch_config()
    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.user,
        password=cfg.password,
        database=cfg.database,
        connect_timeout=5,
        send_receive_timeout=60,
        autogenerate_session_id=False,
    )


def _get_client():
    client = getattr(_local, "client", None)
    if client is None:
        client = _new_client()
        _local.client = client
    return client


def _reset_client() -> None:
    client = getattr(_local, "client", None)
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 關閉失敗不影響重建
            pass
        _local.client = None


def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    """執行 SELECT，回傳 DataFrame。支援 %(key)s 具名參數。

    連線層失敗會重建 client 重試一次；再失敗拋 ChConnectionError。
    """
    head = sql.lstrip().lower()
    if not head.startswith(_ALLOWED_PREFIXES):
        raise ChQueryError("僅允許唯讀查詢（SELECT/WITH/DESCRIBE/SHOW/EXPLAIN）")
    try:
        client = _get_client()
        return client.query_df(sql, parameters=params or {}, settings=_QUERY_SETTINGS)
    except OperationalError:
        logger.warning("ClickHouse 連線層錯誤，重建 client 後重試一次")
        _reset_client()
        try:
            client = _get_client()
            return client.query_df(sql, parameters=params or {}, settings=_QUERY_SETTINGS)
        except OperationalError as exc:
            raise ChConnectionError(f"ClickHouse 連線失敗：{exc}") from exc
        except ClickHouseError as exc:
            raise ChQueryError(f"ClickHouse 查詢失敗：{exc}") from exc
    except ClickHouseError as exc:
        raise ChQueryError(f"ClickHouse 查詢失敗：{exc}") from exc


def query_rows(sql: str, params: dict | None = None) -> list[dict]:
    """同 query()，但回傳 list[dict]（API 層直接序列化用）。"""
    df = query(sql, params)
    return df.to_dict(orient="records")
