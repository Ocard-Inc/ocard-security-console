"""品牌名稱對照：格式、批次快取、以及 MySQL 不可用時的降級行為。

`_fetch` 是唯一碰 MySQL 的地方，因此單元測試一律替換它，不依賴外部連線；
最後一個測試才實際連 MySQL 驗證真實對照（沒設定 MYSQL_HOST 就跳過）。
"""
from __future__ import annotations

import pytest

from console.core import brands
from console.core.config import mysql_config


@pytest.fixture(autouse=True)
def _clean_cache():
    brands.clear_cache()
    yield
    brands.clear_cache()


def test_coerce_id_handles_clickhouse_value_shapes():
    assert brands.coerce_id(7340) == 7340
    assert brands.coerce_id(7340.0) == 7340      # Nullable 欄位經 pandas 會變 float
    assert brands.coerce_id("7340") == 7340
    assert brands.coerce_id(-1) == -1            # brand.idx 實際存在負值
    assert brands.coerce_id(None) is None
    assert brands.coerce_id("") is None
    assert brands.coerce_id("（空）") is None
    assert brands.coerce_id(7340.5) is None
    assert brands.coerce_id(True) is None


def test_label_is_name_then_id(monkeypatch):
    monkeypatch.setattr(brands, "_fetch", lambda ids: {7340: "台灣和民集團"})
    assert brands.label(7340) == "台灣和民集團（7340）"


def test_unknown_id_is_distinguished_from_lookup_failure(monkeypatch):
    monkeypatch.setattr(brands, "_fetch", lambda ids: {})
    assert brands.label(999999) == f"{brands.UNKNOWN_NAME}（999999）"

    brands.clear_cache()
    monkeypatch.setattr(brands, "_fetch", lambda ids: None)
    assert brands.label(999999) == f"{brands.UNAVAILABLE_NAME}（999999）"


def test_lookup_failure_is_not_cached(monkeypatch):
    """MySQL 暫時不可用不該讓品牌名稱在快取存活期間都查不到。"""
    monkeypatch.setattr(brands, "_fetch", lambda ids: None)
    assert brands.label(7340) == f"{brands.UNAVAILABLE_NAME}（7340）"
    monkeypatch.setattr(brands, "_fetch", lambda ids: {7340: "台灣和民集團"})
    assert brands.label(7340) == "台灣和民集團（7340）"


def test_labels_batches_and_caches(monkeypatch):
    calls: list[list[int]] = []

    def fake_fetch(ids):
        calls.append(list(ids))
        return {1: "Ocard 小館", 7340: "台灣和民集團"}

    monkeypatch.setattr(brands, "_fetch", fake_fetch)
    out = brands.labels([7340, 1, 7340, None, "x"])
    assert out == {7340: "台灣和民集團（7340）", 1: "Ocard 小館（1）"}
    assert calls == [[7340, 1]], "同批重複編號只查一次，且略過無法解析的值"

    brands.labels([1, 7340])
    assert len(calls) == 1, "第二次應完全命中快取"


def test_cache_expires(monkeypatch):
    monkeypatch.setattr(brands, "_cache_config", lambda: (0, 20000))
    calls = []
    monkeypatch.setattr(brands, "_fetch",
                        lambda ids: calls.append(list(ids)) or {7340: "台灣和民集團"})
    brands.label(7340)
    brands.label(7340)
    assert len(calls) == 2, "TTL 為 0 時每次都應重新查詢"


def test_empty_input_never_touches_mysql(monkeypatch):
    monkeypatch.setattr(brands, "_fetch",
                        lambda ids: pytest.fail("空輸入不該查 MySQL"))
    assert brands.labels([]) == {}
    assert brands.labels([None, "", "（空）"]) == {}
    assert brands.label(None) == "（空）"


@pytest.mark.skipif(mysql_config() is None, reason="未設定 MYSQL_HOST")
def test_real_lookup_against_mysql():
    """實際對照 MySQL：7340 是 README 記載的 7/16 事件品牌。"""
    out = brands.labels([7340, 1])
    assert out[7340].endswith("（7340）")
    assert brands.UNKNOWN_NAME not in out[7340]
    assert brands.UNAVAILABLE_NAME not in out[7340]
    assert out[1].endswith("（1）")
