"""Explorer 的「有沒有錯誤」一律走 `exprs.API_HAS_ERROR`，不可自己寫比對。

## 這個 bug 的實測樣貌（2026-08-07）

`ods_api_log` 在 2026-08-05 重建後 `has_error` 變成 `Nullable(String)`
（實測值只有 NULL、`'1'`、`'verify failed'`）。`exprs.py` 當時已經改成
`isNotNull(has_error)` 並寫下「**唯一真相，不要在各處自己寫比對**」，
但 `queries/explorer.py` 有三處被漏掉，全部還在跟整數 1 比：

- `where_clause()` 的 `only_error` → 勾「只看有 error」直接 502
- `error_analysis()` 的 `countIf(has_error = 1)` → 「失敗／錯誤分析」直接 502
  （畫面上是「查詢失敗」而不是圖 —— 使用者看到的就是「圖出不來」）
- `_detail_row()` 的 `r.get("has_error") == 1` → **這一個不會報錯**：
  值是字串 `'1'`，`== 1` 永遠 False，於是逐筆明細的「結果」欄
  **每一筆都顯示「成功」**，包括真正出錯的那些。

前兩個是大聲失敗，第三個是靜靜給錯答案 —— 後者才是這個檔案存在的主要理由。
"""
from __future__ import annotations

import pytest

from console.queries import explorer, exprs


def test_explorer_never_compares_has_error_to_an_integer():
    """原始碼裡不可以再出現 `has_error = 1` 這種型別相依的比對。

    用原始碼掃描而不是只測行為，是因為第三處（逐筆明細的「結果」欄）
    **不會報錯**，只會把每一筆都標成「成功」。
    """
    src = (explorer.__file__)
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    offenders = [ln.strip() for ln in code.splitlines()
                 if "has_error" in ln and ("= 1" in ln or "==1" in ln or "== 1" in ln)
                 and not ln.strip().startswith("#")]
    assert not offenders, (
        "這些地方還在拿 has_error 跟整數比，欄位已是 Nullable(String)：\n  "
        + "\n  ".join(offenders))


def test_error_analysis_actually_runs(client):
    """「失敗／錯誤分析」必須查得動 —— 它壞掉時畫面只有「查詢失敗」，沒有圖。"""
    r = client.post("/api/explorer", json={
        "source": "api", "start": "2026-08-06 21:00:00", "end": "2026-08-06 22:00:00",
        "analysis": "error", "limit": 500, "bucket": "auto", "only_error": False,
    })
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert "rows" in body
    for row in body["rows"]:
        # 錯誤數不可能超過總數 —— 超過就是兩邊算在不同的母體上
        assert 0 <= row["errors"] <= row["total"], row


def test_only_error_filter_actually_runs(client):
    """勾「只看有 error」必須查得動，而且結果一定是總量的子集。"""
    base = {"source": "api", "start": "2026-08-06 21:00:00",
            "end": "2026-08-06 22:00:00", "analysis": "endpoint",
            "limit": 500, "bucket": "auto"}
    everything = client.post("/api/explorer", json={**base, "only_error": False})
    only_err = client.post("/api/explorer", json={**base, "only_error": True})
    assert everything.status_code == 200, everything.text[:300]
    assert only_err.status_code == 200, only_err.text[:400]
    total_all = sum(r["count"] for r in everything.json()["rows"])
    total_err = sum(r["count"] for r in only_err.json()["rows"])
    assert total_err <= total_all, "只看錯誤的筆數不可能多於全部"


def test_detail_result_column_uses_the_shared_definition():
    """逐筆明細的「結果」欄要跟 SQL 用同一個定義（非 NULL = 有錯誤）。

    這一條守的是那個**不會報錯**的分支：字串 `'1'` 與整數 1 比永遠 False，
    於是每一筆都被標成「成功」。
    """
    assert exprs.API_HAS_ERROR == "isNotNull(has_error)"

    def result_of(has_error):
        row = {"create_time": "2026-08-06 21:00:00", "_id": 1, "has_error": has_error,
               "controller": "Api2", "function": "GetProfile", "platform": None,
               "headers": None, "params": None, "status": None,
               "order_number": None, "_admin": None, "_brand": 1180, "_store": 0}
        return explorer._mask_detail_row("api", row)["result"]

    row_err = {"result": result_of("1")}
    row_verify = {"result": result_of("verify failed")}
    row_ok = {"result": result_of(None)}
    assert row_err["result"] == "錯誤"
    assert row_verify["result"] == "錯誤", "非 NULL 一律算錯誤（同 SQL 的定義）"
    assert row_ok["result"] == "成功"
