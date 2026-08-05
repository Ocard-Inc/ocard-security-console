"""R07A 必須看得見兩個登入家族。

`Boss_initial/auth_v2`（2026-08 的新版登入端點）**不寫 `acc` 欄位**，帳號在
`params` 這個 JSON 字串的 `acc` 鍵裡。R07A 原本的 SQL 有
`AND acc IS NOT NULL AND acc != ''`，於是佔登入流量 77% 的新版端點完全沒有
帳號層監測 —— 不報錯，只是永遠不告警。

行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py 的做法）：
有人把條件改回只看 `acc` 欄位的話，這裡會失敗。
"""
from __future__ import annotations

from console.core.ch import query
from console.rules.loader import load_rules

# 2026-08-05 實測：這個視窗內 Boss_initial/auth_v2 有 login_failed，
# 而它們的 acc 欄位全部是 NULL。
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 18:20:00"}


def _rule(rid: str):
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def test_new_login_family_has_null_acc_column():
    """前提事實：新版登入端點的 acc 欄位是空的。

    這個測試存在的理由是「前提消失時要大聲」—— 哪天上游開始寫 acc 欄位了，
    R07A 的 JSONExtract 就變成沒必要的複雜度，而這裡會告訴我們。
    """
    df = query(
        "SELECT count() AS n, countIf(acc IS NULL OR acc = '') AS no_acc"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND function = 'Boss_initial/auth_v2' AND action = 'login_failed'",
        WINDOW)
    total, no_acc = int(df["n"][0]), int(df["no_acc"][0])
    assert total > 0, "這個視窗應該有新版端點的登入失敗紀錄"
    assert no_acc == total, (
        f"新版端點開始寫 acc 欄位了（{total} 筆裡只有 {no_acc} 筆是空的）—— "
        "R07A 的 JSONExtract 可以簡化，但要先確認兩個家族都還抓得到")


def test_r07a_sees_both_login_families():
    """R07A 的 SQL 必須同時抓到兩個家族的帳號。

    只抓到 legacy 家族的話這個斷言會失敗 —— 那正是修這條規則之前的狀態。
    """
    rule = _rule("R07A")
    df = query(rule.sql, WINDOW)
    assert not df.empty, "這個視窗應該有帳號達到 R07A 的 HAVING 門檻"
    accounts = set(df["acc"])
    assert "" not in accounts and None not in accounts, (
        "R07A 吐出了空帳號 —— WHERE 的判空條件沒有跟著改成對 JSONExtract "
        "之後的運算式判斷，那會產生一個在 Explorer 查不到東西的對象")
    # 新版端點的帳號只存在於 params 裡；抓到任何一個就證明兩個家族都看得見。
    from_params = query(
        "SELECT DISTINCT JSONExtractString(params, 'acc') AS acc"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND function = 'Boss_initial/auth_v2' AND action = 'login_failed'"
        "   AND JSONExtractString(params, 'acc') != ''",
        WINDOW)
    new_family = set(from_params["acc"])
    assert new_family, "前提：新版端點的 params 裡有 acc"
    assert accounts & new_family, (
        f"R07A 沒有抓到任何新版登入端點的帳號。它抓到 {sorted(accounts)[:5]}，"
        f"而新版端點的帳號是 {sorted(new_family)[:5]} —— "
        "SQL 的 acc 還是只讀欄位，沒有 fallback 到 params")


def test_r07a_does_not_select_raw_params():
    """絕不可以把整段 params 選進輸出。

    實測樣本裡有 `pwd`（MD5 hash）與 `push_token`，而 `masking.scrub_text()` 的
    清洗清單只有 authorization / cookie / secret / api_key —— **沒有 pwd**。
    規則的 context 會進 Slack 與磁碟上的 state/logs/*.log。
    """
    rule = _rule("R07A")
    df = query(rule.sql, WINDOW)
    assert "params" not in df.columns, (
        "R07A 的 SQL 輸出了 params 欄位 —— 那會讓密碼 hash 流進 events.context、"
        "Slack 訊息與磁碟上的 log。只 JSONExtract 需要的那一個鍵。")


def test_params_acc_usage_is_confined_to_new_login_family():
    """Guard the premise: params.acc 只在 Boss_initial/auth_v2 登入動作出現。

    explorer.py:52 的三層 coalesce 依賴一個假設：params.acc 只用來儲存操作者帳號。
    如果未來某個新的 function 把 params.acc 當目標帳號用，或其他用途，這個假設就破掉了，
    而 GROUP_BY["actor"]["admin"] 會把整個資料來源的列誤認成不同的操作者。

    28 天實測（2026-07-08 ~ 08-06）：
    ① params.acc 非空且 acc 為空，**只在這兩個 function/action**：
       - Boss_initial/auth_v2/login_success （219K 筆）
       - Boss_initial/auth_v2/login_failed （3.6K 筆）
    ② acc 有值且 params.acc 也有值時，兩者 100% 相同 —— 不存在矛盾。
    """
    # 測試區間擴大到 28 天，確保涵蓋足夠的樣本
    lookback = {"start": "2026-07-08 00:00:00", "end": "2026-08-06 00:00:00"}

    # 檢查 1：params.acc 非空且 acc 為空的列，只出現在兩個特定動作
    empty_acc_with_params = query(
        "SELECT DISTINCT concat(function, '/', action) AS fn_action"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND (acc IS NULL OR acc = '')"
        "   AND JSONExtractString(params, 'acc') != ''",
        lookback)

    fn_actions = set(empty_acc_with_params["fn_action"])
    expected_actions = {"Boss_initial/auth_v2/login_success", "Boss_initial/auth_v2/login_failed"}
    assert fn_actions <= expected_actions, (
        f"params.acc 被用在意外的 function/action 組合裡！預期只在 {expected_actions}，"
        f"但還出現在 {fn_actions - expected_actions}。這代表 params.acc 可能有了第二種意義，"
        f"GROUP_BY[\"actor\"][\"admin\"] 的全域 fallback 會把這些列誤認成錯誤的操作者。")

    # 檢查 2：當 acc 有值且 params.acc 也有值時，兩者必須完全相同
    both_present = query(
        "SELECT count() AS total,"
        "       countIf(acc = JSONExtractString(params, 'acc')) AS matching"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND acc != '' AND acc IS NOT NULL"
        "   AND JSONExtractString(params, 'acc') != ''",
        lookback)

    total = int(both_present["total"][0])
    matching = int(both_present["matching"][0])
    assert total == 0 or matching == total, (
        f"acc 和 params.acc 出現矛盾！{total} 筆列中只有 {matching} 筆相同。"
        f"這代表 params.acc 可能被用來記錄**目標**帳號而非操作者，"
        f"explorer.py:52 的全域 fallback 會對誰是操作者給出錯誤的答案。")
