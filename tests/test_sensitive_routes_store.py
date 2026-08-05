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
        assert sr.disable(route, who="b@olis.com.tw") is True
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


def test_disable_a_route_that_is_not_there_returns_false():
    assert sr.disable("nope_test/nope", who="a@olis.com.tw") is False


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
