"""2026-08-07 接入的五張表：可查（Explorer）+ 可看（健康卡 / sparkline）。

每張表一組測試。共用的 `assert_source_works()` 走一遍「使用者真的會做的事」——
比對照表的存在性檢查更嚴格：對照表齊全但運算式寫錯的話，
`tests/test_data_source_coverage.py` 會過而這裡會失敗。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.core.config import settings
from console.queries import explorer, source_schema


def _recent_window(days: int = 3) -> tuple[str, str]:
    """最近 N 天的台北牆鐘區間。新表的資料都是 2026-08-06 之後才有的。"""
    end = timewin.effective_now()
    return timewin.fmt(end - timedelta(days=days)), timewin.fmt(end)


def _explore(client, source: str, analysis: str, days: int = 3):
    """`/api/explorer` 是 **POST**（不是 GET）。"""
    start, end = _recent_window(days)
    return client.post("/api/explorer", json={
        "source": source, "start": start, "end": end, "analysis": analysis})


def assert_source_works(client, source: str, *, expect_analyses: set[str]) -> None:
    """一個來源「接好了」的完整定義。Task 5–9 共用。"""
    # ① 綱要存在且表名與 settings 一致
    schema = source_schema.get(source)
    assert schema.table == settings()["data_sources"][source]["table"]

    # ② Explorer 的能力清單有這個來源，且宣告的分析方式與預期相同
    meta = {m["key"]: m for m in explorer.source_meta()}
    assert source in meta, f"explorer.source_meta() 沒有 {source}"
    assert set(meta[source]["analyses"]) == expect_analyses, (
        f"{source} 宣告的分析方式與預期不同："
        f"{sorted(meta[source]['analyses'])} != {sorted(expect_analyses)}")

    # ③ 宣告支援的分析方式**真的跑得起來**。宣告了卻 400/502 是最糟的形狀：
    #    畫面上是個正常選項，點下去壞掉。
    for analysis in meta[source]["analyses"]:
        r = _explore(client, source, analysis)
        assert r.status_code == 200, (
            f"{source}/{analysis} → {r.status_code} {r.text[:300]}")

    # ④ 健康卡有這一張，而且不是「查詢失敗」
    cards = {c["key"]: c for c in client.get("/api/health").json()["sources"]}
    assert source in cards, f"/api/health 沒有 {source}"
    assert cards[source]["status"] != "查詢失敗", (
        f"{source} 的健康卡查詢失敗：{cards[source].get('error')}")
    assert cards[source]["note"], f"{source} 的健康卡沒有資料限制說明"

    # ⑤ sparkline 有這一條
    assert source in client.get("/api/sparklines").json()["sources"]

    # ⑥ 不支援的篩選必須說出原因，不可以是空字串或一句「不支援」
    for field, reason in meta[source]["unsupported_filters"].items():
        assert reason and len(reason) > 10, (
            f"{source} 的 {field} 不支援，但原因寫得太短：{reason!r}")


# ── batch（ods_batch_request_log）─────────────────────────────────────────────

def test_batch_source_works(client):
    """批次匯入排程（im.ocard.co）。

    ip 實測 100% 是 0.0.0.0、input 100% 空，所以沒有來源與操作者維度。
    """
    assert_source_works(client, "batch",
                        expect_analyses={"trend", "endpoint", "detail"})


def test_batch_has_no_source_ip_dimension():
    """反向：`ip` 欄位存在但恆為 0.0.0.0，不可以假裝它是來源。

    有人「順手補齊」的話，來源排名會出現一個佔 100% 的 0.0.0.0，
    而那會被讀成「所有請求都來自同一個 IP」—— 完全錯誤的結論。
    """
    assert "batch" not in explorer.GROUP_BY["source"]
    reason = explorer.filter_support("source_ip", "batch")
    assert reason and "0.0.0.0" in reason, (
        "拒絕的理由必須說出「ip 欄位有值但恆為 0.0.0.0」，"
        f"否則下一個人會以為只是漏掉了：{reason!r}")


@pytest.mark.parametrize("source", tuple(settings()["data_sources"]))
def test_every_registered_source_has_a_detail_branch(source):
    """`_mask_detail_row()` 的每個**已註冊**來源都要有自己的分支。

    原本最後一支是 catch-all `else:  # auth`。已註冊但沒有分支的來源會靜靜掉
    進去：endpoint 取 `action`、操作者取 `token`，新表兩個欄位都沒有，於是整張
    明細變成一列列的 None —— 而畫面看起來只是「這些欄位剛好是空的」。

    未註冊的來源本來就會在 `settings()[...]["label"]` 那裡 KeyError，
    所以真正需要守的是**已註冊**這一側。
    """
    # 假的一列，欄位**從 `_DETAIL_COLUMNS` 解析**（那正是這個分支實際會收到的
    # 欄位集合）。不能用 defaultdict —— `_mask_detail_row` 開頭的 dict
    # comprehension 會用 `.items()` 重建成普通 dict，空的 defaultdict 會變成空 dict。
    # `X AS create_time` 這種別名取最後一段。
    cols = [c.strip().split()[-1]
            for c in explorer._DETAIL_COLUMNS[source].split(",")]
    row = explorer._mask_detail_row(source, {c: None for c in cols})
    assert set(row) >= {"row_id", "time", "source", "endpoint", "actor",
                        "source_ip", "result", "params", "resource"}, (
        f"{source} 的明細列缺欄位，可能沒有自己的分支：{sorted(row)}")


def test_unknown_source_fails_loudly_in_detail_rows():
    """未註冊的來源必須大聲失敗，不可以靜靜渲染成別的表的形狀。"""
    with pytest.raises((KeyError, ValueError)):
        explorer._mask_detail_row("nonexistent_source", {"create_time": None})
