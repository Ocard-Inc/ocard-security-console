"""`_admin` 編號 → 帳號名的對照。

三件事必須守住，違反了都不會報錯：
① ReplacingMergeTree 的舊版本要被 FINAL 去掉，否則同一個編號回兩列、
   dict 被後到的舊版本蓋掉；
② 查不到不可以假裝（回一個看起來像帳號的值）；
③ 查詢失敗要降級、不可以往上拋 —— 名稱是輔助資訊，不該讓整個明細 500。
"""
from __future__ import annotations

import pytest

from console.core import admins

# 2026-08-06 實測：Order Log 一天 2,887 個相異 _admin 100% 對得到帳號。
# 26465 當天出現 10,247 次，排第三多（第一、二名分別是 42990 的 14,535 次、
# 26361 的 11,531 次）。
KNOWN_ADMIN = 26465
KNOWN_ACCOUNT = "cp07_pos"

# 2026-08-06 實測：ods_user_admin 的去重鍵是 (_brand, idx)，FINAL 之後這個
# idx 仍跨 _brand 回兩列（8649 → 舊的 ocardjack、NULL → _version 較新的
# jack@ocard.co，create_time 相同、是同一個人）。正確答案是 _version 最新
# 的那一列。
BRAND_SPANNING_ADMIN = 30058
BRAND_SPANNING_ACCOUNT = "jack@ocard.co"


@pytest.fixture(autouse=True)
def _clean_cache():
    admins.clear_cache()
    yield
    admins.clear_cache()


def test_resolves_a_known_admin_id():
    out = admins.accounts([KNOWN_ADMIN])
    assert out[KNOWN_ADMIN] == KNOWN_ACCOUNT


def test_final_dedupes_replacingmergetree_versions():
    """`ods_user_admin` 實測 59,293 列只有 41,300 個相異 idx —— 舊版本還在。

    不加 FINAL 的話同一個 idx 會回兩列，而批次組 dict 時後到的（可能是舊版本）
    會蓋掉先到的。症狀是「帳號名偶爾是舊的」，沒有任何錯誤訊息。

    `FINAL` 只是其中一半：去重鍵是 `(_brand, idx)` 不是 `idx`，跨 `_brand`
    的收斂靠 `argMax(acc, _version)` + `GROUP BY idx`（見
    `test_collapses_idx_that_spans_multiple_brands`）。
    """
    assert "FINAL" in admins._SQL_TEMPLATE, (
        "查詢沒有 FINAL —— ods_user_admin 是 ReplacingMergeTree，"
        "同一個 (_brand, idx) 會回多列")
    out = admins.accounts([KNOWN_ADMIN, KNOWN_ADMIN, 26466])
    assert set(out) == {KNOWN_ADMIN, 26466}


def test_collapses_idx_that_spans_multiple_brands():
    """`ods_user_admin` 的去重鍵是 `(_brand, idx)`，不是單獨的 `idx`。

    `FINAL` 只在同一個 `(_brand, idx)` 之內去重，跨 `_brand` 的同一個 `idx`
    不會被合併 —— 實測全表 `FINAL` 後仍有 4 個 idx 各回 2 列（1、19323、
    30058、43137）。那不是「兩個不同帳號」：兩列的 create_time 完全相同，
    是同一個人的兩條 ODS 同步路徑（一條帶 _brand、一條 _brand 是 NULL），
    帳號改名時只有其中一條抓到。

    這則測試守的是：查詢必須用 `argMax(acc, _version)` + `GROUP BY idx`
    跨 `_brand` 收斂成一列、且取 `_version` 最新的那個值 —— 不是退回
    `FINAL` + `iterrows()` 覆寫（那個版本的最終值取決於 ClickHouse 回傳列的
    順序，順序沒有被強制，症狀是帳號名不穩定且不會報錯）。
    """
    out = admins.accounts([BRAND_SPANNING_ADMIN])
    assert len(out) == 1
    assert out[BRAND_SPANNING_ADMIN] == BRAND_SPANNING_ACCOUNT

    admins.clear_cache()
    out_again = admins.accounts([BRAND_SPANNING_ADMIN])
    assert out_again[BRAND_SPANNING_ADMIN] == BRAND_SPANNING_ACCOUNT, (
        "呼叫兩次應得到相同結果 —— 穩定性是這個函式的契約，"
        "不是碰運氣碰到對的那一列")


def test_only_selects_idx_and_acc():
    """那張表還有 pwd／vtoken／email／tel／ip —— 一個都不該進主控台。

    `name` 也刻意不取：`_store` 已經有自己的 `store_label`，把店名再帶一份
    會讓同一列出現兩個店名。
    """
    for forbidden in ("pwd", "vtoken", "email", "tel", "ip", "name"):
        assert forbidden not in admins._SQL_TEMPLATE, (
            f"SQL 取了不該取的欄位 {forbidden}")


def test_unknown_admin_is_not_faked():
    out = admins.accounts([999_999_999])
    assert out[999_999_999] == admins.UNKNOWN_NAME


def test_query_failure_degrades_instead_of_raising(monkeypatch):
    """查詢失敗時整批回「查詢失敗」，而不是半真半假，也不是拋例外。"""
    monkeypatch.setattr(admins, "_fetch", lambda ids: None)
    out = admins.accounts([KNOWN_ADMIN])
    assert out[KNOWN_ADMIN] == admins.UNAVAILABLE_NAME


def test_unparseable_values_are_skipped_not_crashed():
    """事件 context 存的是 float（pandas 把純數值列升成 float64），
    而排名的 k 是字串。兩種都要吃得下；真的解不出整數的就不出現在結果裡。"""
    out = admins.accounts([str(KNOWN_ADMIN), float(KNOWN_ADMIN), None, "", "abc"])
    assert out[KNOWN_ADMIN] == KNOWN_ACCOUNT
    assert len(out) == 1


def test_second_call_uses_the_cache(monkeypatch):
    admins.accounts([KNOWN_ADMIN])
    calls = []
    monkeypatch.setattr(admins, "_fetch", lambda ids: calls.append(ids) or {})
    admins.accounts([KNOWN_ADMIN])
    assert not calls, "快取沒有生效，每次都打 ClickHouse"


def test_single_lookup_helper():
    assert admins.account(KNOWN_ADMIN) == KNOWN_ACCOUNT
    assert admins.account(None) == "（空）"
