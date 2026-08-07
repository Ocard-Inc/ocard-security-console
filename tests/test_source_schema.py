"""來源綱要：每個 data_source 都要有，且既有來源的行為必須完全不變。"""
from __future__ import annotations

import pytest

from console.core.config import settings
from console.queries import exprs, source_schema

SOURCES = tuple(settings()["data_sources"])

# 綱要引入之前就存在的五個來源。它們的時間欄位都叫 create_time、都是台北牆鐘，
# 所以綱要對它們產出的 SQL 必須與改動前一字不差。
LEGACY = ("api", "backend", "admin", "auth", "order")


def test_every_data_source_has_a_schema():
    """漏一個的症狀是 KeyError → 走到它的端點直接 500。"""
    for key in SOURCES:
        assert key in source_schema.SCHEMAS, (
            f"source_schema.SCHEMAS 少了 {key} —— 那是 KeyError 而不是 "
            "ChQueryError，會讓走到它的端點直接 500。")


def test_schema_table_matches_settings():
    """表名的唯一真相仍是 settings.yaml，綱要不可以有第二份不一致的副本。"""
    for key in SOURCES:
        assert source_schema.get(key).table == settings()["data_sources"][key]["table"]


@pytest.mark.parametrize("source", LEGACY)
def test_legacy_sources_keep_the_exact_same_time_filter(source):
    """既有五張表存的就是台北牆鐘，條件字串必須與改動前一字不差。

    差一個字都代表這個改動動到了既有行為 —— 而那正是它刻意不做的事。
    """
    assert exprs.time_filter_for(source) == (
        "create_time >= %(start)s AND create_time < %(end)s")


@pytest.mark.parametrize("source", LEGACY)
def test_legacy_sources_have_no_dedup_order(source):
    """只有 ods_request_log 的同一鍵會有多個版本，其餘一律單純 count()。"""
    assert source_schema.get(source).dedup_order is None


def _partition_key(table: str) -> str:
    from console.core.ch import query_rows
    rows = query_rows(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase()"
        " AND name = %(t)s", {"t": table})
    assert rows, f"system.tables 查不到 {table}"
    return str(rows[0]["partition_key"] or "")


@pytest.mark.parametrize("source", SOURCES)
def test_time_filter_actually_prunes_partitions(source):
    """時間條件必須打得到分區鍵，否則長區間查詢會靜靜退化成全表掃描。

    UTC 的表用 `toDateTime(%(start)s, 'Asia/Taipei')` 轉換 —— 這則測試的重點就是
    確認「**包了一層函式之後裁剪還在**」。實測 ods_ec_request_log 六天的區間：
    44 parts → 2 parts、180 granules → 6。

    退化的症狀不是報錯，而是選長區間時查詢慢到撞 55 秒上限，
    使用者看到的是「查詢超時」，看不出原因。

    期望值**由 `system.tables.partition_key` 推導，不寫死** —— 有些表根本沒有
    分區（見下一則測試），要求它們裁剪只會產生一個永遠紅的假警報。
    """
    from console.core.ch import query_rows

    schema = source_schema.get(source)
    pk = _partition_key(schema.table)
    if schema.time_col not in pk:
        pytest.skip(f"{schema.table} 的分區鍵是 {pk!r}，不含 {schema.time_col} "
                    "—— 由 test_tables_without_a_time_partition 記錄")

    plan = "\n".join(
        str(next(iter(r.values()))) for r in query_rows(
            f"EXPLAIN indexes=1 SELECT count() FROM {schema.table}"
            f" WHERE {source_schema.time_filter(source)}",
            {"start": "2026-08-01 00:00:00", "end": "2026-08-07 00:00:00"}))
    assert "Partition" in plan, (
        f"{source} 的時間條件沒有觸發分區裁剪，而它的分區鍵是 {pk!r} —— "
        f"time_filter 可能把欄位包在裁剪穿不過的運算式裡。EXPLAIN：\n{plan}")


def test_tables_without_a_time_partition_are_known():
    """哪些表沒有時間分區，要明寫出來，不可以靜靜跳過。

    **`ods_admin_log` 完全沒有分區鍵**（`partition_key` 是空字串），而且它的
    sorting key 是 `_brand, _admin, _id` 也不含時間 —— 所以對它的每一個查詢都是
    全表掃描，任何時間條件都只能靠逐列過濾。這不是可以修的東西（要改上游的
    表定義），但它決定了「這張表的查詢能開多寬」，所以必須寫下來。

    這則測試的用途是**反向**的：哪天上游替它加了分區鍵，這裡會失敗，
    提醒下一個人回頭把上面那則 skip 拿掉、並重新評估區間上限。
    """
    no_partition = {
        key for key in SOURCES
        if source_schema.get(key).time_col not in _partition_key(
            source_schema.get(key).table)}
    assert no_partition == {"admin"}, (
        f"「沒有時間分區的表」變了：{sorted(no_partition)}。"
        "多了一張代表新接的來源掃全表（要重新評估區間上限）；"
        "少了一張代表上游加了分區鍵（可以把對應的 skip 拿掉）。")
