"""`ods_api_log.has_error` 的兩個消費端：「只看有 error」與明細的 `result`。

## 為什麼這個檔案存在

`has_error` 曾經是數值，2026-08-05 `ods_api_log` 重建後變成 `Nullable(String)`
（實測值只有 NULL、`'1'`、`'verify failed'` 三種）。`queries/exprs.API_HAS_ERROR`
是「這一筆是錯誤回應」的唯一真相（`isNotNull(has_error)`），但 `queries/explorer.py`
有三處沒有跟上，各自壞成不同的樣子：

- `where_clause()` 的 `only_error` → `has_error = 1` 在 SQL 端拋
  code 386 NO_COMMON_TYPE。**對所有區間都是死的**，不只近期。
- `error_analysis()` 的 `countIf(has_error = 1)` → 同上，整支分析失敗。
- `_mask_detail_row()` 的 `r.get("has_error") == 1` → Python 端永遠 False，
  **明細把每一筆錯誤的請求都顯示成「成功」**。

前兩者至少會拋錯；第三個是這個專案最怕的那一類 —— 不報錯，只是靜靜給出錯的
結論，而畫面完全正常。它能活這麼久，正是因為 `only_error` 與明細的 `result`
**一則測試都沒有**（`error_analysis` 只是碰巧被 `supported_analyses()` 的
「列出來的分析都要跑得起來」間接蓋到）。

所以這個檔案守的不是「功能有沒有做」，而是**那三處不可以再退回型別相關的比對**。

## 為什麼不只斷言「有錯誤列」

`isNotNull` 被改成恆真（例如有人「簡化」成 `1` 或 `has_error IS NOT NULL OR 1`）
時，「有錯誤列」照樣通過。所以兩個方向都要斷言：篩選後**全部**是錯誤、
不篩時**同時存在**成功與錯誤。

## 區間是寫死的歷史時段，這是刻意的

`2026-08-06 00:00 ~ 06:00` 實測 13,621 筆裡有 24 筆錯誤
（`Webhook/Shopline` 錯誤率 9.84%、`Webhook/Cyberbiz` 5.26%）。
**不用「今天」或「最近 N 小時」** —— 這個計畫已經出現過一則因此每天壞一小時的
時間相依測試（見 `tests/test_explorer_trend_window.py` 的
`test_future_tail_is_not_drawn_as_zero_and_says_so`）。
歷史 log 不會被覆寫，所以寫死的區間比滾動視窗穩定。
"""
from __future__ import annotations

from console.queries import explorer

# 實測有錯誤的穩定歷史區間（見模組說明）
WINDOW = {"start": "2026-08-06 00:00:00", "end": "2026-08-06 06:00:00"}


def _detail(**overrides) -> dict:
    f = explorer.ExplorerFilter(source="api", limit=50, **WINDOW, **overrides)
    return explorer.detail(f)


def test_only_error_returns_rows_and_every_one_is_an_error():
    """`only_error=True` 的每一列都必須是「錯誤」。

    `has_error = 1` 的年代這個查詢在 SQL 端就拋 code 386，所以「回得出列」
    本身就是一個回歸守門；而「每一列都是錯誤」才擋得住恆真的條件。
    """
    d = _detail(only_error=True)
    assert d["total"] > 0, (
        f"{WINDOW} 應該有錯誤請求（實測 24 筆）—— 回 0 筆代表 only_error 的"
        "條件壞了（或這個區間的資料被改動過，那要重新挑區間而不是放寬斷言）")
    results = {r["result"] for r in d["rows"]}
    assert results == {"錯誤"}, (
        f"only_error=True 卻出現非錯誤的列：{sorted(results)} —— "
        "`_mask_detail_row()` 的 result 判斷與 `where_clause()` 的 only_error "
        "條件對 has_error 的解讀不一致")


def test_unfiltered_detail_has_both_outcomes():
    """不篩時同時要有「成功」與「錯誤」。

    這是上一則的反面：只斷言「篩選後全是錯誤」的話，一個把**每一列**都判成
    「錯誤」的實作（例如 `pd.NA is not None` 為真那種寫法）也會通過。
    """
    d = _detail()
    results = {r["result"] for r in d["rows"]}
    assert "成功" in results, (
        f"這個區間的明細一列「成功」都沒有：{sorted(results)} —— "
        "result 判斷可能把 NULL（正常）也算成錯誤了")
    assert "錯誤" in results, (
        f"這個區間的明細一列「錯誤」都沒有：{sorted(results)} —— "
        "result 判斷可能永遠回「成功」（那正是 `== 1` 在 Nullable(String) 上的症狀）")


def test_only_error_actually_narrows():
    """篩選要真的縮小結果，而不是回同一批。"""
    assert _detail(only_error=True)["total"] < _detail()["total"]


def test_error_analysis_runs_and_reports_a_rate():
    """`error_analysis()` 在 `has_error = 1` 的年代整支失敗（SQL code 386）。"""
    rows = explorer.error_analysis(
        explorer.ExplorerFilter(source="api", **WINDOW))["rows"]
    assert rows, "這個區間應該算得出錯誤率（實測 Webhook/Shopline 9.84%）"
    for r in rows:
        assert r["errors"] > 0, f"HAVING errors > 0 卻回了 0：{r}"
        assert 0 < r["error_rate"] <= 1, (
            f"錯誤率不是 0..1 的小數：{r} —— 比例值一律以小數流動，"
            "顯示時才由 web/lib.js 的 pct() 乘 100")
