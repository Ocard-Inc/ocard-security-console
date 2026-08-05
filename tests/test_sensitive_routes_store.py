"""敏感路由清單的資料層。

這份清單原本在 config/settings.yaml，兩個讀取端（R05 的 SQL 與掃描的 P03 探針）
各有一份副本。搬進 SQLite 是為了能從後台編輯 —— 而**移除一條敏感路由就是製造
盲區**，所以逐列留痕（誰加的、誰停的、為什麼）。

`tests/conftest.py` 的 `state_db` 已經把 DB 換成 tmp 的複本，所以這裡的寫入
不會碰到真實的 state/monitor.db（`tests/test_db_isolation.py` 守著這件事）。
"""
from __future__ import annotations

from console.core.config import settings
from console.store import db, migrate, sensitive_routes as sr


def test_seed_puts_the_settings_list_into_the_table():
    """播種：settings.yaml 的清單必須全部在表裡且生效中。"""
    seeded = set(settings()["sensitive_routes"])
    assert seeded, "settings.yaml 的 sensitive_routes 是空的 —— 前提不成立"
    in_table = set(sr.active())
    missing = seeded - in_table
    assert not missing, f"這幾條沒有被播種進表：{sorted(missing)}"


def test_seed_is_idempotent():
    """跑第二次不可以新增任何列。

    播種掛在 db.get_conn()（部署流程沒有地方插一次性 CLI），而連線是
    thread-local —— 排程器 thread、FastAPI threadpool 的每條 thread、
    每個 CLI process 都會各跑一次。
    """
    before = len(sr.all_rows())
    migrate.seed_after_schema(db.get_conn())
    db.get_conn().commit()
    assert len(sr.all_rows()) == before


def _restore_row(original: dict) -> None:
    """把一列還原成停用前的原始內容（明列全部七欄，直接 UPDATE）。

    刻意不透過 `sr.add()` 還原：`add()` 對既有列的 UPDATE 是「重新啟用」的
    語意 —— 有一個真人現在重新核准這條路由，所以 `added_by`/`added_at`/
    `reason` **應該**跟著換成這次核准的人與理由。測試 teardown 要的是相反的
    事：假裝什麼都沒發生過，把 provenance 全部復原成原始值。這張表存在的
    唯一理由就是回答「這條是誰加的、為什麼」，teardown 寫錯值等於悄悄
    偽造那個答案 —— 而且不會有任何斷言失敗，因為復原後 status 一樣是
    生效中，下一次讀 `active()` 完全看不出差異。
    """
    with db.tx() as conn:
        conn.execute(
            "UPDATE sensitive_routes SET status = ?, added_by = ?, added_at = ?,"
            " reason = ?, removed_by = ?, removed_at = ? WHERE route = ?",
            (original["status"], original["added_by"], original["added_at"],
             original["reason"], original["removed_by"], original["removed_at"],
             original["route"]))


def test_seed_does_not_resurrect_a_manually_disabled_route():
    """人工停用的路由不可以被下一次啟動悄悄復活。

    同 intel/refresh.seed_allowlist() 那個去重檢查刻意不看 status 的理由：
    人工停用的核准不可被每日排程在隔天 06:00 悄悄復活。
    這裡的版本更嚴重 —— 復活一條路由會讓 R05 與掃描重新看它，
    而使用者以為自己已經關掉了。
    """
    target = settings()["sensitive_routes"][0]
    original = sr.get(target)
    assert original is not None, f"{target} 應該已經被播種進表 —— 前提不成立"
    try:
        sr.disable(target, who="test@olis.com.tw")
        assert target not in sr.active()
        migrate.seed_after_schema(db.get_conn())
        db.get_conn().commit()
        assert target not in sr.active(), (
            f"{target} 被播種復活了 —— INSERT OR IGNORE 必須不看 status")
        row = sr.get(target)
        assert row["status"] == sr.STATUS_DISABLED
        assert row["removed_by"] == "test@olis.com.tw"
        assert row["removed_at"]
    finally:
        # 直接 UPDATE 還原成停用前的原始內容，不經 sr.add()（見 _restore_row
        # 的說明）—— 否則這條路由的 added_by/reason 會從「seed / settings.yaml
        # 初始清單」被永久改寫成這個測試留下的痕跡，而 status 一樣是生效中，
        # 沒有任何斷言會失敗，錯誤要等到後續讀 added_by 的測試才會現形。
        _restore_row(original)


def test_add_then_disable_then_reactivate():
    """新增 → 停用 → 重新啟用，全程只有一列。"""
    route = "zzz_test/route"
    try:
        assert sr.add(route, who="a@olis.com.tw", reason="測試") == "created"
        assert route in sr.active()
        assert sr.disable(route, who="b@olis.com.tw") == sr.DISABLE_OK
        assert route not in sr.active()
        assert sr.add(route, who="c@olis.com.tw",
                      reason="測試重啟") == "reactivated"
        row = sr.get(route)
        assert row["status"] == sr.STATUS_ACTIVE
        assert row["added_by"] == "c@olis.com.tw", "重新啟用要更新是誰啟用的"
        assert row["removed_by"] is None, "重新啟用要清掉上一次的停用紀錄"
    finally:
        with db.tx() as conn:
            conn.execute("DELETE FROM sensitive_routes WHERE route = ?", (route,))


def test_add_on_an_already_active_route_does_not_touch_provenance():
    """POST 一條已經生效中的路由必須回 ADD_ALREADY_ACTIVE，且完全不動那一列。

    早期版本的 `add()` 只看 INSERT OR IGNORE 有沒有失敗，失敗就無條件 UPDATE
    成生效中並改寫 added_by/added_at/reason —— 對一條**本來就生效中**的路由
    （典型是 added_by='seed'、reason='settings.yaml 初始清單' 的種子列）重複
    呼叫，會把它的來源紀錄靜靜改寫成這次呼叫的值，而 API 端點還會照樣宣稱
    「恢復敏感路由」，那件事根本沒有發生 —— 它從來沒被停用過。
    """
    target = settings()["sensitive_routes"][0]
    before = sr.get(target)
    assert before is not None and before["status"] == sr.STATUS_ACTIVE, (
        f"前提：{target} 應該是播種進來的生效中路由")
    outcome = sr.add(target, who="intruder@olis.com.tw", reason="不該生效的重複新增")
    assert outcome == sr.ADD_ALREADY_ACTIVE
    after = sr.get(target)
    assert after == before, (
        "add() 對一條生效中的路由回 ADD_ALREADY_ACTIVE 時，那一列必須完全"
        "沒被動到 —— added_by/added_at/reason 的 provenance 不可被覆寫")


def test_disable_a_route_that_is_not_there_returns_not_found():
    assert sr.disable("nope_test/nope", who="a@olis.com.tw") == sr.DISABLE_NOT_FOUND


def test_disable_refuses_the_last_active_route_and_leaves_it_untouched():
    """`disable()` 的「還有沒有別的生效中路由」與「真的執行停用」必須是同一顆
    UPDATE，不是「呼叫端先讀 active_count() 再判斷」。

    後者在兩個併發呼叫打**不同**路由時會被繞過（兩者都在檢查時讀到同一個
    「還有 2 條」，都通過、都真的停用，清單被清空）——這是 2026-08 review
    抓到的 race。這裡不試著用真的執行緒重現那個時序（不確定性太高，monkeypatch
    也騙不出真正的 SQLite 寫入鎖行為），而是直接驗證這個函式本身要守住的
    不變量：只剩一條生效中的路由時，直接呼叫 store 層的 `disable()` 也必須
    拒絕，而且完全不動那一列（不只 status 不變，`removed_by`/`removed_at`
    也不能被寫入——半途寫壞比什麼都沒做更難察覺）。
    """
    active = sr.active()
    assert len(active) > 1, "前提：至少兩條，否則這個測試會真的清空清單"
    originals = {route: sr.get(route) for route in active}
    disabled: list[str] = []
    try:
        for route in active[:-1]:
            outcome = sr.disable(route, who="test@olis.com.tw")
            assert outcome == sr.DISABLE_OK, outcome
            disabled.append(route)
        last = active[-1]
        before_row = sr.get(last)
        outcome = sr.disable(last, who="test@olis.com.tw")
        assert outcome == sr.DISABLE_LAST_ACTIVE
        after_row = sr.get(last)
        assert after_row == before_row, "拒絕時那一列必須完全沒被動到"
        assert sr.active() == [last]
    finally:
        for route in disabled:
            _restore_row(originals[route])


def test_active_is_sorted_and_only_active():
    routes = sr.active()
    assert routes == sorted(routes), "active() 要排序（清單顯示與 SQL 都靠它穩定）"
    statuses = {r["status"] for r in sr.all_rows() if r["route"] in set(routes)}
    assert statuses == {sr.STATUS_ACTIVE}


def test_all_rows_lists_every_column_explicitly():
    """讀取端一律明列欄位，不可用 row.get(col, default)。

    「欄位不存在」與「值是 NULL」在語意上會撞在一起 —— removed_by 的 NULL 是
    「沒有被停用過」。欄位沒建成功時每一列都靜靜變成「沒被停用過」而畫面正常。
    """
    row = sr.all_rows()[0]
    assert set(row) == {"route", "status", "added_by", "added_at", "reason",
                        "removed_by", "removed_at"}


def test_exprs_reads_the_store_not_the_yaml():
    """exprs.sensitive_routes() 必須回表的內容。

    簽名刻意不變（回 list[str]），所以呼叫端一行都不用改。
    """
    from console.queries import exprs
    assert exprs.sensitive_routes() == sr.active()


def test_p03_sql_has_no_hardcoded_route_literals():
    """P03 的 SQL 不可以把清單內插進字串。

    probes() 有 lru_cache(maxsize=1)，內插的話探針表會凍結在 server 啟動時的
    清單 —— 於是 R05 立即生效而掃描要重啟，而畫面上兩邊都正常。
    這正是「一份清單兩邊一起生效」要避免的事。
    """
    from console.sweep.probes import probes
    p03 = next(p for p in probes() if p.id == "P03")
    assert "%(sensitive_routes)s" in p03.sql, (
        "P03 的 SQL 沒有用 %(sensitive_routes)s 參數")
    for route in sr.active():
        assert f"'{route}'" not in p03.sql, (
            f"P03 的 SQL 裡有寫死的路由字面值 {route!r} —— "
            "lru_cache 會讓它凍結在啟動時的清單")


def test_sweep_build_params_supplies_the_live_list():
    from datetime import datetime
    from console.sweep import run
    params = run.build_params(datetime(2026, 8, 1), datetime(2026, 8, 2))
    assert params["sensitive_routes"] == sr.active()


def test_p03_declares_that_it_needs_the_route_list():
    """P03 要標記 needs_sensitive_routes，否則 run_probes 不知道要跳過它。"""
    from console.sweep.probes import probes
    p03 = next(p for p in probes() if p.id == "P03")
    assert p03.needs_sensitive_routes is True


def test_limits_reports_blocking_when_the_list_is_empty(monkeypatch):
    """空清單要在「資料限制」以 blocking 明說沒有檢查。

    實測 ClickHouse 的 `IN []` **不報錯、回 0 筆** —— 那與「這段期間沒有敏感
    路由存取」在畫面上一模一樣，而後者是結論、前者是「我們沒在看」。
    把「沒有資料」說成「沒有發生」是這個專案一再警告的錯誤。

    直接驗 `limits.collect()` 而不是跑一整趟 `run_probes()`：後者會執行全部
    探針（實測單次含 API 探針約 30 秒），而要驗的是降級文案本身。
    """
    from console.core import timewin
    from console.sweep import limits, run
    monkeypatch.setattr(limits.exprs, "sensitive_routes", lambda: [])
    empty_run = run.ProbeRun(hits=(), timings_ms={}, failures={},
                             skipped=("P03",), params={})
    items = limits.collect(timewin.parse("2026-08-04 00:00:00"),
                           timewin.parse("2026-08-05 00:00:00"), empty_run)
    hit = [i for i in items if i.key == "sensitive_routes_empty"]
    assert hit, "空清單沒有產生限制項目 —— 那等於靜靜回報「沒有異常」"
    assert hit[0].level == "blocking", (
        f"空清單的限制等級是 {hit[0].level!r}，必須是 blocking")
    assert "沒有執行" in hit[0].detail
