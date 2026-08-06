"""API routes。所有回應皆為遮罩後資料；權限於 server 端強制。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from console.alerting import notify
from console.api import drilldown, validate
from console.auth import ros
from console.auth.roles import CurrentUser, current_user, guard
from console.core import brands, masking, stores, timewin
from console.core.ch import ChConnectionError, ChQueryError
from console.core.config import settings
from console.queries import (
    brand_search, endpoint_suggest, entity, entity_history, explorer, health,
    quick_templates, sparklines, store_search, trends,
)
from console.rules import effective
from console.rules.loader import load_rules
from console.store import allowlist, audit, db, rule_overrides, sensitive_routes, sweeps
from console.intel import store as intel_store
from console.sweep import narrate
from console.sweep import report as sweep_report

logger = logging.getLogger(__name__)
router = APIRouter()

_SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 調查判定的允許值。前端 web/pages/event-detail.js 的 JUDGEMENTS 是同一組字，
# 而 /events 的 judgement 篩選也讀它 —— 提交端與篩選端必須是同一個常數，
# 否則會出現「存得進去但篩不出來」的判定值。
JUDGEMENTS = ("已確認攻擊", "合法整合", "誤報", "證據不足", "保持觀察")

# 「還沒有人判定」在篩選器裡的值。events.judgement 存的是 NULL，而 NULL 沒辦法
# 當成查詢字串傳，所以給它一個顯示用的值。**它不是可以提交的判定** ——
# judge_event 只接受 JUDGEMENTS。
UNJUDGED = "待判定"

# 清單頁的判定頁籤。key 是穩定的機器識別（進網址）、label 是畫面文字。
#
# **成員清單只存在這裡，前端從回應讀、不自己列一份。** 前端若有第二份，日後新增
# 第六個判定值時，那個判定的事件會從所有頁籤一起消失，而畫面完全正常
# （test_judgement_tabs_cover_every_judgement 擋這件事）。
#
# `judgements` 非空 = 限定為那些值；**空 = 不加判定條件**（只有「全部」那格）。
# UNJUDGED 本來就是 list_events 接受的篩選值（語意是 judgement IS NULL），
# 所以「待判定」那格不需要額外的欄位或分支。
#
# 「已排除」這個名字是挑過的：「已確認」會被讀成「已確認攻擊」的上層分類，
# 於是使用者以為攻擊事件也在裡面 —— 兩個頁籤名字互相吃掉；而「證據不足」其實
# 什麼都沒確認。「已結案」也不能用 —— status='closed' 的顯示字就是「已處理完畢」，
# 同一個詞指兩件事（resolved 曾經在清單叫「已停止」、在篩選器叫「已恢復」）。
#
# 「全部」放最後一格：進站預設落在待判定（分流優先）。它存在的理由是既有入口
# 沒有誠實的落點 —— 總覽的 P0–P3 卡片顯示的是**不分判定**的筆數，那些事件散在
# 各格裡，少了這格就會出現「卡片上寫 3，點進去只有 1」。
JUDGEMENT_TABS = (
    {"key": "unjudged", "label": UNJUDGED, "judgements": [UNJUDGED]},
    {"key": "attack", "label": "已確認攻擊", "judgements": ["已確認攻擊"]},
    {"key": "watch", "label": "保持觀察", "judgements": ["保持觀察"]},
    {"key": "excluded", "label": "已排除",
     "judgements": ["合法整合", "誤報", "證據不足"]},
    {"key": "all", "label": "全部", "judgements": []},
)

# judgement_note 的三個欄位。自 2026-08 起全部選填，所以空字串是正常值。
_JUDGEMENT_FIELDS = ("reason", "evidence", "next_step")

# events.status 的三個值。active / resolved 由狀態機寫，closed（已處理完畢）
# 只由人寫 —— 見 store/events.py 的模組說明。這裡是 /events 篩選的白名單：
# 打錯一律 400，靜靜接受的話 `status=closd` 回 0 筆而畫面寫著「狀態 = closd」。
STATUSES = ("active", "resolved", "closed")
CLOSED = "closed"


# ─────────────────────────── 會話與導覽 ───────────────────────────

@router.get("/session")
def session(user: CurrentUser = Depends(current_user)) -> dict:
    """目前登入者。沒有角色分級 —— 進得來就有全部功能。"""
    return {
        "email": user.email,
        "name": user.name,
        # 顯示 ROS 那邊的角色名稱（管理員、資訊主管…），不是主控台自己的等級
        "role_label": user.role_label,
        "env_label": settings()["app"]["env_label"],
        "timezone": settings()["time"]["timezone"],
        # dev = 未接 ROS 的離線模式，前端據此顯示警示
        "auth_source": user.source,
        "logout_url": f"{ros.base_url()}/api/auth/signout" if ros.enabled() else None,
        "ros_url": ros.base_url() if ros.enabled() else None,
    }


# ─────────────────────────── 資安總覽 ───────────────────────────

# 排名查詢的視窗上限。趨勢查詢跨五張表（含 Order Log）在 7 天視窗只要 0.7 秒
# （月分區 + part 級 create_time 剪枝很有效；四張表時實測 0.5 秒），但 sources 排名要對
# headers 做 JSONExtract，7 天要掃 19M 列花 3.2 秒 —— 佔整個 /overview 的大半。
# 排名夾在 24 小時，前端據 window_minutes 誠實標示。
RANKING_MAX_MINUTES = 1440


# 自訂區間的上限。每張表都有 create_time 範圍剪枝，但 sources 排名要對 headers
# 做 JSONExtract，區間拉太長會讓單一請求跑上好幾十秒。
MAX_CUSTOM_RANGE_DAYS = 31


def _parse_window(start: str | None, end: str | None) -> tuple:
    """自訂區間的共用解析與驗證。兩個都給才算數；只給一個視同沒給。"""
    if not start or not end:
        return None, None
    try:
        s, e = timewin.parse(start), timewin.parse(end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if s >= e:
        raise HTTPException(400, "開始時間必須早於結束時間")
    span_days = (e - s).total_seconds() / 86400
    if span_days > MAX_CUSTOM_RANGE_DAYS:
        raise HTTPException(
            400, f"自訂區間最長 {MAX_CUSTOM_RANGE_DAYS} 天，目前為 {span_days:.1f} 天")
    return s, e


@router.get("/overview")
def overview(
    minutes: int = Query(60, ge=10, le=10080),
    # 自訂區間（台北牆鐘）。兩個都給時蓋過 minutes。
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_overview")
    started = time.time()
    win_start, win_end = _parse_window(start, end)
    if win_start is not None:
        minutes = max(int((win_end - win_start).total_seconds() // 60), 10)
    cards = health.source_health()
    fresh = health.freshness_summary(cards)

    counts = {s: 0 for s in _SEV_ORDER}
    ongoing = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, status, COUNT(*) AS n FROM events"
            " WHERE first_seen >= ? GROUP BY severity, status",
            (timewin.fmt(timewin.taipei_now() - timedelta(hours=24)),)):
        counts[row["severity"]] = counts.get(row["severity"], 0) + row["n"]
        if row["status"] == "active":
            ongoing[row["severity"]] = ongoing.get(row["severity"], 0) + row["n"]

    prev = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, COUNT(*) AS n FROM events"
            " WHERE first_seen >= ? AND first_seen < ? GROUP BY severity",
            (timewin.fmt(timewin.taipei_now() - timedelta(hours=48)),
             timewin.fmt(timewin.taipei_now() - timedelta(hours=24)))):
        prev[row["severity"]] = row["n"]

    attention = db.rows(
        "SELECT * FROM events WHERE status = 'active' AND severity IN ('P0','P1','P2')"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,"
        " metric_value DESC LIMIT 5")

    # 「已結束但從未判定」的積壓。事件會因為數值回到門檻以下而自動 resolved，
    # 但 attention 只查 status='active'，所以一旦自動結束就從首頁完全消失 ——
    # 首頁於是顯示「沒有未處理事件」，即使沒有任何人看過它們。那是假的安心感，
    # 跟本專案「沒有事件 ≠ 系統安全」的前提正好相反。
    # 不設時間下限：這是待辦積壓不是時間視窗。純 SQLite，零 ClickHouse 成本。
    pending_by_sev = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, COUNT(*) AS n FROM events"
            " WHERE judgement IS NULL GROUP BY severity"):
        pending_by_sev[row["severity"]] = row["n"]
    pending_rows = db.rows(
        "SELECT * FROM events WHERE judgement IS NULL"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1"
        " WHEN 'P2' THEN 2 ELSE 3 END, first_seen DESC LIMIT 5")
    oldest = db.one("SELECT MIN(first_seen) AS m FROM events WHERE judgement IS NULL")

    hb = db.one("SELECT * FROM heartbeat WHERE key = 'five_min'")
    daily = db.one("SELECT * FROM heartbeat WHERE key = 'daily'")
    monitor_status = _monitor_status(hb, fresh)

    return {
        # 我們自己關掉的東西必須出現在**人們真的會看的那一頁**，而不是只在一個
        # 要主動點進去的管理頁。少了這一段，「16 條規則有 3 條被停用、5 個來源
        # 被抑制」就只有進到規則頁的人知道 —— 那正是靜靜的盲區。
        # 純 SQLite，零 ClickHouse 成本；語意是「不限時間的現況」。
        "suppression": _suppression_summary(),
        "severity_cards": [
            {"severity": s, "count": counts.get(s, 0),
             "diff": counts.get(s, 0) - prev.get(s, 0),
             "ongoing": ongoing.get(s, 0)} for s in ("P0", "P1", "P2", "P3")],
        "monitor": monitor_status,
        "last_five_min_check": hb["last_ok"] if hb else None,
        "last_daily_check": daily["last_ok"] if daily else None,
        "trend": trends.request_trend(minutes=minutes, start=win_start, end=win_end),
        "attention": [_event_public(e) for e in attention],
        "pending_judgement": {
            "total": sum(pending_by_sev.values()),
            "by_severity": pending_by_sev,
            "oldest": oldest["m"] if oldest else None,
            "events": [_event_public(e) for e in pending_rows],
        },
        "health": cards,
        "freshness": fresh,
        "rankings": trends.risk_rankings(
            minutes=min(minutes, RANKING_MAX_MINUTES), end=win_end),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _suppression_summary() -> dict:
    """「我們自己關掉了什麼」的現況。給資安總覽的橫幅用。"""
    try:
        rules = effective.effective_rules()
    except Exception:                                    # noqa: BLE001
        # YAML 壞掉不該讓整個總覽 500 —— 那會讓一個設定錯誤變成全站不可用
        logger.exception("讀取生效規則失敗（總覽的抑制摘要降級）")
        # slack 仍要帶：它不依賴規則檔，而「通知送不出去」在 YAML 壞掉的時候
        # 更需要被看見（那時規則一條都沒在跑）。敏感路由清單同理：它不依賴
        # 規則檔，YAML 壞掉時更需要看見這個盲區。
        return {"available": False, "slack": notify.summary(),
                "active_sensitive_routes": sensitive_routes.active_count(),
                "disabled_sensitive_routes": sensitive_routes.disabled_count()}
    entries = allowlist.active_entries()
    soon = settings()["allowlist"]["expiring_soon_days"]
    expiring = [e for e in entries if e.valid_to and 0 <= (
        timewin.parse(e.valid_to) - timewin.taipei_now()).days <= soon]
    return {
        "available": True,
        # 「通知送不出去」與停用規則、Allowlist 屬於同一類事實：監測還在跑，
        # 但結論到不了人身上。放在同一個橫幅裡才不會需要第二個地方去看。
        "slack": notify.summary(),
        "disabled_rules": [r.id for r in rules if not r.enabled],
        "overridden_rules": sorted(rule_overrides.all_overrides()),
        "active_allowlist": len(entries),
        "global_allowlist": sum(1 for e in entries if e.rule_id is None),
        "no_expiry": sum(1 for e in entries if not e.valid_to),
        "expiring_soon": len(expiring),
        "expiring_soon_days": soon,
        # 移除一條敏感路由是刻意的盲區：R05 與期間掃描同時停止看那條路由。
        # 少了這個數字，那件事就只有進到規則頁的人知道。
        "active_sensitive_routes": sensitive_routes.active_count(),
        "disabled_sensitive_routes": sensitive_routes.disabled_count(),
    }


# 心跳超過幾個 tick 沒被更新就算中斷。與 resolve_after_ticks 同一個數量級：
# 連續三次沒動靜已經不是抖動。
STALE_TICK_MULTIPLE = 3


def _monitor_status(hb: dict | None, fresh: dict) -> dict:
    if hb is None:
        return {"label": "尚未執行", "color": "#98A2B3",
                "note": "五分鐘檢查尚未執行過，目前無法判定是否沒有異常。"}
    # 「心跳本身沒更新」必須先判，而且要判在 consecutive_failures 之前。
    # 這一列的內容只有 run_tick 會寫；process 死掉、排程器 thread 卡住、
    # 或例外在寫心跳之前就逃出去的話，讀到的是**上一次成功**的內容 ——
    # consecutive_failures 是 0、note 是空的，於是畫面顯示綠色「正常」。
    # 那是這個主控台最糟的失效模式：它宣稱「沒有異常」，而它根本沒在看。
    stale = _heartbeat_stale_minutes(hb)
    if stale is not None:
        return {"label": "監測中斷", "color": "#B42318",
                "note": f"五分鐘檢查已 {stale} 分鐘沒有執行"
                        f"（最後一次 {hb['last_tick'] or '未知'}），"
                        f"畫面上的數字停在那個時間點，現在無法判定是否沒有異常。"}
    if hb["consecutive_failures"] >= 1:
        return {"label": "監測失敗", "color": "#B42318",
                "note": f"五分鐘檢查連續失敗 {hb['consecutive_failures']} 次"
                        f"（{hb['note'] or '未知原因'}），現在無法判定是否沒有異常。"}
    if fresh["failed"]:
        return {"label": "部分失敗", "color": "#B42318",
                "note": f"{'、'.join(fresh['failed'])} 查詢失敗，結果不完整。"}
    if fresh["delayed"]:
        return {"label": "部分延遲", "color": "#DC6803",
                "note": fresh["banner"]}
    if hb["note"]:
        return {"label": "部分規則失敗", "color": "#DC6803", "note": hb["note"]}
    return {"label": "正常", "color": "#027A48", "note": ""}


def _heartbeat_stale_minutes(hb: dict) -> int | None:
    """心跳落後幾分鐘（未超過容許值回 None）。解析不了時間一律當成中斷。"""
    limit = settings()["time"]["tick_minutes"] * STALE_TICK_MULTIPLE
    if not hb["last_tick"]:
        return limit
    try:
        age = (timewin.taipei_now() - timewin.parse(hb["last_tick"])).total_seconds() / 60
    except ValueError:
        return limit
    return int(age) if age > limit else None


# ─────────────────────────── 異常事件 ───────────────────────────

def _event_public(e: dict) -> dict:
    ctx = json.loads(e["context_json"]) if e.get("context_json") else {}
    return {
        "evt_no": e["evt_no"], "rule_id": e["rule_id"], "rule_name": e["rule_name"],
        "severity": e["severity"], "entity_label": e["entity_label"],
        "source": e["source_key"], "metric": e["metric_value"],
        "threshold": e["threshold"], "median": e["baseline_median"],
        "p95": e["baseline_p95"], "multiple": e["multiple"], "brands": e["brands"],
        # 「涉及品牌 N 個」的展開明細（偵測當下算好存進 context，見 rules/engine.py）
        "brand_top": ctx.get("brand_top") or [],
        "first_seen": e["first_seen"], "last_seen": e["last_seen"],
        "peak": e["peak_value"], "hit_count": e["hit_count"],
        "status": e["status"], "judgement": e["judgement"],
        # 人工結案。closed_from 是關閉當下狀態機的值 —— 清單上要能說出
        # 「這件事是在還在持續命中的時候被結案的」，那與「回落之後才結案」
        # 是兩種不同的事。
        "closed_at": e["closed_at"], "closed_by": e["closed_by"],
        "closed_from": e["closed_from"],
        "case_no": e["case_id"], "owner": e["owner"], "context": ctx,
    }


def _judgement_detail(raw: str | None) -> dict:
    """判定當下填的理由／證據／下一步。

    **三個欄位自 2026-08 起選填，所以空字串是正常值、不是資料缺損。**
    這個函式存在的理由是那三個欄位原本是**只寫不讀**的：judge_event 寫進
    judgement_note，而 `_event_public` 沒有回傳它，畫面上沒有任何地方看得到。
    既然填不填變成使用者的選擇，就必須看得見填了什麼 —— 不然「選填」等於
    「打了字也沒人會看到」。

    刻意只在詳細頁算（同 drilldown 的理由）：`_event_public` 也服務 /events
    一次 300 列與 /overview，那兩處用不到這段自由文字。

    舊資料可能不是 JSON（欄位比現在的表單更早存在）—— 那時整段放進 reason
    而不是丟掉，那是別人寫下的調查紀錄。
    """
    empty = {k: "" for k in _JUDGEMENT_FIELDS}
    if not raw:
        return empty
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {**empty, "reason": raw}
    if not isinstance(parsed, dict):
        return {**empty, "reason": raw}
    return {k: str(parsed.get(k) or "").strip() for k in _JUDGEMENT_FIELDS}


_EVENT_LIMIT = 300


def _judgement_filter(values: list[str] | None, unjudged: bool,
                      tab: str | None) -> list[str]:
    """正規化 judgement / tab 參數 → 一組判定值（空清單 = 不篩判定）。

    **可重複**：`?judgement=合法整合&judgement=誤報&judgement=證據不足`
    ——「已排除」那個頁籤要三個值。

    `tab` 是頁籤 key 的簡寫，展開成該格的成員。它存在的理由是**前端不該知道
    成員清單**：清單頁的網址是 `#/events?tab=excluded`，第一次查詢在拿到
    `judgement_tabs` 之前就要送出，若要前端自己展開，那份成員就得寫死在前端 ——
    正是 JUDGEMENT_TABS 的註解在擋的那件事。兩個一起給是 400（不定義誰蓋誰，
    免得出現「網址寫 A、結果是 B」而畫面完全正常）。

    值是封閉集合，**打錯一律 400**：靜靜接受的話 `judgement=誤報x` 回 0 筆，
    而畫面上的已套用條件寫著「判定 = 誤報x」，讀起來像「這段時間沒有誤報」。
    """
    picked = list(dict.fromkeys(v for v in (values or []) if v))   # 去重、保序
    if tab is not None:
        found = next((t for t in JUDGEMENT_TABS if t["key"] == tab), None)
        if found is None:
            raise HTTPException(
                400, f"tab 必須是 {'／'.join(t['key'] for t in JUDGEMENT_TABS)} 之一"
                     f"（收到 {tab!r}）")
        if picked or unjudged:
            raise HTTPException(400, "tab 與 judgement／unjudged 不可同時給"
                                     "（tab 本身就是一組判定）")
        return list(found["judgements"])
    unknown = [v for v in picked if v not in JUDGEMENTS and v != UNJUDGED]
    if unknown:
        raise HTTPException(
            400, f"judgement 必須是 {'、'.join((*JUDGEMENTS, UNJUDGED))} 之一"
                 f"（收到 {'、'.join(repr(v) for v in unknown)}）")
    # 條件之間是 OR，但「還沒有人判定」與「判定是誤報」不可能同時成立於同一筆，
    # 而混著送的人要的多半是別的東西。靜靜回傳兩者聯集會讓「待判定」那格突然
    # 多出已判定的事件 —— 大聲擋掉。
    if UNJUDGED in picked and len(picked) > 1:
        raise HTTPException(
            400, f"{UNJUDGED} 不可與其他判定混用"
                 f"（收到 {'、'.join(picked)}）")
    if unjudged:
        # unjudged=true 是 UNJUDGED 的別名（總覽的「前往判定」連結在用）。
        # 與具體判定同時給永遠是 0 筆，靜靜回 0 的話使用者的結論會是
        # 「這段時間沒有這種判定」，而其實是自己把兩個入口都打開了。
        if picked and picked != [UNJUDGED]:
            raise HTTPException(
                400, f"unjudged=true 與 judgement={'、'.join(picked)} 互相矛盾"
                     f"（{UNJUDGED} 與已判定不可能同時成立）")
        return [UNJUDGED]
    return picked


def _facets(where: str, params: tuple) -> dict:
    """嚴重度 × 狀態的真實計數（**不受 LIMIT 影響**）。

    刻意不從清單那份 rows 數：rows 有 LIMIT 300，撞到上限時「共 N 筆事件」
    與四個嚴重度數字會**靜靜少算**，而畫面上沒有任何跡象。
    """
    rows = db.rows(f"SELECT severity, status, COUNT(*) AS n FROM events"
                   f" WHERE {where} GROUP BY severity, status", params)
    return {
        "total": sum(r["n"] for r in rows),
        "by_severity": {s: sum(r["n"] for r in rows if r["severity"] == s)
                        for s in _SEV_ORDER},
        "by_status": {k: sum(r["n"] for r in rows if r["status"] == k)
                      for k in STATUSES},
    }


def _judgement_tabs(where: str, params: tuple) -> list[dict]:
    """頁籤與其筆數 —— **套用其他篩選、不套用判定條件**。

    這是頁籤上的數字，範圍與下方清單刻意不同：清單是「這一格有什麼」，頁籤是
    「同樣的條件下，別格還有幾筆」。所以呼叫端傳進來的 where **不含判定條件**。

    舊的 `by_judgement` 是「套用**全部**篩選之後」算的，於是套用判定篩選時其餘
    每一格必然是 0。同一個鍵兩種範圍是這個專案一再出事的形狀，所以那個鍵直接
    刪掉，跨判定的計數只活在這裡 —— 在這裡它的意思只可能是「頁籤上的數字」。
    """
    rows = db.rows(f"SELECT COALESCE(judgement, ?) AS j, COUNT(*) AS n"
                   f" FROM events WHERE {where} GROUP BY j", (UNJUDGED, *params))
    counts = {r["j"]: r["n"] for r in rows}
    return [{"key": t["key"], "label": t["label"],
             "judgements": list(t["judgements"]),
             "count": (sum(counts.values()) if not t["judgements"]
                       else sum(counts.get(j, 0) for j in t["judgements"]))}
            for t in JUDGEMENT_TABS]


@router.get("/events")
def list_events(
    severity: str | None = None,
    status: str | None = None,
    rule_id: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    # 調查判定，**可重複**（見 _judgement_filter）。JUDGEMENTS 之一 = 只看該判定；
    # UNJUDGED = 還沒有人判定；完全不給 = 不篩（「全部」那個頁籤）。
    judgement: list[str] | None = Query(None),
    # 頁籤 key（judgement 的簡寫，見 _judgement_filter）。清單頁的網址帶的是這個。
    tab: str | None = None,
    # UNJUDGED 的別名。資安總覽的「前往判定」連結與 test_api_smoke 在用，
    # 保留是為了不破那個契約；新的呼叫端一律用 judgement。
    unjudged: bool = False,
    hours: int = Query(168, ge=1, le=24 * 90),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_events")
    # 四個封閉集合一律驗證。清單頁的條件現在寫在網址裡、使用者改得到，而靜靜
    # 接受一個不存在的值會回 0 筆，配上畫面「嚴重度 = P9」讀起來像
    # 「這段時間沒有 P9」——「值不存在」與「沒有事件」必須分得開。
    for name, val, allowed in (("severity", severity, tuple(_SEV_ORDER)),
                               ("status", status, STATUSES),
                               ("source", source, tuple(settings()["data_sources"]))):
        if val and val not in allowed:
            raise HTTPException(400, f"{name} 必須是 {'、'.join(allowed)} 之一"
                                     f"（收到 {val!r}）")
    wanted = _judgement_filter(judgement, unjudged, tab)
    clauses = ["first_seen >= ?"]
    params: list = [timewin.fmt(timewin.taipei_now() - timedelta(hours=hours))]
    for col, val in (("severity", severity), ("status", status),
                     ("rule_id", rule_id), ("source_key", source)):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if keyword:
        clauses.append("(entity_label LIKE ? OR rule_name LIKE ? OR evt_no LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    # 頁籤的數字用的是**這一份**（不含判定條件）—— 它要回答「別格還有幾筆」。
    tabs = _judgement_tabs(" AND ".join(clauses), tuple(params))
    # 「待判定」是 judgement IS NULL 而不是某個字串，三個入口（總覽橫幅的
    # unjudged 連結、網址的 tab=unjudged、判定下拉）都落在這一條。
    if wanted == [UNJUDGED]:
        clauses.append("judgement IS NULL")
    elif wanted:
        clauses.append(f"judgement IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    where = " AND ".join(clauses)
    facets = _facets(where, tuple(params))
    rows = db.rows(
        f"SELECT * FROM events WHERE {where}"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1"
        f" WHEN 'P2' THEN 2 ELSE 3 END, first_seen DESC LIMIT {_EVENT_LIMIT}",
        tuple(params))
    return {
        "events": [_event_public(r) for r in rows],
        # total 是**真實筆數**，不是 len(events)：頁籤數字也是真實計數，
        # 兩者放在同一個畫面上而其中一個被 LIMIT 截斷的話會直接打架
        # （「待判定 512」配「共 300 筆事件」）。截斷要說出來，不是靜靜少算。
        "total": facets["total"],
        "shown": len(rows),
        "truncated": len(rows) < facets["total"],
        "by_severity": facets["by_severity"],
        "by_status": facets["by_status"],
        "judgement_tabs": tabs,
        "judgements": list(JUDGEMENTS),
        "unjudged_label": UNJUDGED,
        "ongoing": facets["by_status"]["active"],
    }


@router.get("/events/{evt_no}")
def event_detail(
    evt_no: str,
    # 趨勢圖往事件視窗前後各再拉多久。只看前後 30 分鐘看不出事件之前的脈絡。
    pad_minutes: int = Query(30, ge=10, le=10080),
    # 自訂絕對區間（台北牆鐘）。兩個都給時蓋過 pad_minutes。
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_events")
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    win_start, win_end = _parse_window(start, end)
    audit.record(who=user.email, role=user.role_label, action="查看事件", target=evt_no)
    event = _event_public(row)
    rule = next((r for r in load_rules() if r.id == row["rule_id"]), None)
    event["rule_note"] = rule.note if rule else ""
    # 「在 Log Explorer 查此對象」的篩選條件。刻意只在詳細頁算，不放進
    # `_event_public()` —— 後者也服務 /events（一次 300 列）與 /overview，
    # 而這裡完全用不到清單頁的成本與風險（見 api/drilldown.py 模組說明）。
    event["drilldown"] = drilldown.build(rule, event)
    event["judgement_detail"] = _judgement_detail(row["judgement_note"])
    event["allowlist_prefill"] = _allowlist_prefill(rule, event)
    event["allowlist_matches"] = _allowlist_matches(rule, event)
    event["evidence"] = _build_evidence(row, event)
    event["limitations"] = _extra_limitations(event) + _data_limitations(row["source_key"])
    event["trend"] = _event_trend(row, pad_minutes=pad_minutes,
                                  start=win_start, end=win_end)
    return event


def _entity_context(evt_no: str) -> tuple[dict, object, object | None, str | None]:
    """`(events 列, 規則, EntityRef|None, 不適用的原因)`。

    對象視角的兩個端點共用。`EntityRef` 一律由 `drilldown.build()` 的結果推導 ——
    「規則 entity → 篩選欄位」的唯一真相在那裡，這裡不重做判定
    （重做就會有兩份會漂移的規則，而漂移的症狀是面板靜靜查到 0 筆）。
    """
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    rule = next((r for r in load_rules() if r.id == row["rule_id"]), None)
    event = _event_public(row)
    dd = drilldown.build(rule, event)
    if not dd.get("supported"):
        return row, rule, None, dd.get("reason") or "這個事件的對象無法反查。"
    ref = entity.from_filters(row["source_key"], dd.get("filter") or {})
    if ref is None:
        # R09（entity 是字面常數 scope）與 R12（沒有 entity）走這裡。
        # 這不是缺陷，是「這條規則沒有可追蹤的對象」—— 必須照實說，
        # **不可以退回畫全站圖假裝有內容**。
        return row, rule, None, (
            f"{row['rule_name']}沒有可追蹤的對象"
            "（它的偵測範圍是整張表，不是某個帳號或來源），因此不提供對象面板。")
    # 對象條件是否真的比對得到記錄，只有執行期問得出來（規則的 entity 可以是
    # SQL 裡的字面常數，見 entity.unresolved_reason）。**兩個端點都要擋** ——
    # 時序那支用同一個 ref，漏了它會畫出一條 28 天全 0 的線。
    # 多一趟 count（實測 0.2 秒）換掉三塊各自編出錯誤說法的面板。
    end = timewin.parse(row["last_seen"])
    window = rule.window_minutes if rule else 60
    try:
        unresolved = entity.unresolved_reason(
            ref, end - timedelta(minutes=window), end, row["metric_value"])
    except ChQueryError as exc:
        # 查不動不等於不成對 —— 說出「無法確認」，不要假裝面板是好的，
        # 也不要因此把一個正常的對象判成不成對（同 explorer 的 explain_failed）。
        return row, rule, None, f"無法確認這個事件的對象是否可查詢：{exc}"
    if unresolved:
        return row, rule, None, unresolved
    return row, rule, ref, None


# 刻意用同步 def，不是 async def（同 /sweep 與 /explorer/payload）。裡面的
# ClickHouse 查詢是阻塞的，寫成 async def 會佔住事件迴圈、連五分鐘排程一起卡住。
@router.get("/events/{evt_no}/entity")
def event_entity(evt_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    """對象視角的三個便宜面板：母體位置、24 小時作息、端點來源集中度。

    與事件詳細頁分開的端點，讓頁面先畫得出來（實測這裡合計約 3 秒）。
    """
    guard(user, "view_events")
    row, rule, ref, reason = _entity_context(evt_no)
    if ref is None:
        return {"supported": False, "reason": reason}

    end = timewin.parse(row["last_seen"])
    window = rule.window_minutes if rule else 60
    try:
        peers = entity.peers(ref, end - timedelta(minutes=window), end,
                             expected=row["metric_value"])
        profile = entity.hour_profile(ref, end)
        share = entity.endpoint_share(ref, end)
    except ChQueryError as exc:
        return {"supported": False, "reason": f"對象面板查詢失敗：{exc}"}
    return {
        "supported": True,
        "label": ref.label,
        "dims": [{"field": d.field, "label": d.label, "value": _display_dim(d)}
                 for d in ref.dims],
        "window_minutes": window,
        "peers": peers, "profile": profile, "share": share,
    }


@router.get("/events/{evt_no}/entity/timeline")
def event_entity_timeline(
    evt_no: str,
    days: int = Query(entity_history.TIMELINE_DAYS, ge=2, le=90),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """對象自己的長期時序（面板 A）。實測 28 天約 5–7 秒 → 前端延後載入。"""
    guard(user, "view_events")
    row, _rule, ref, reason = _entity_context(evt_no)
    if ref is None:
        return {"supported": False, "reason": reason}
    try:
        data = entity_history.timeline(
            ref, timewin.parse(row["first_seen"]),
            timewin.parse(row["last_seen"]), days=days)
    except ChQueryError as exc:
        return {"supported": False, "reason": f"對象時序查詢失敗：{exc}"}
    return {"supported": True, "label": ref.label, **data}


def _display_dim(dim) -> str:
    return dim.value if dim.mask is None else (
        masking.DISPLAY_FUNCS[dim.mask](dim.value) or dim.value)


def _allowlist_prefill(rule, event: dict) -> dict:
    """「判定為合法整合 → 建立例外」要用的預填值。

    **IP 由後端提供，不可由前端解析 `entity_label`。** 那個字串是
    `acc · ip` 用 ` · ` 串起來的（見 rules/engine.entity_parts），依賴它的格式
    遲早會錯，而錯的症狀是「建了一筆永遠不生效的例外」。

    形式比照 drilldown 的 supported / reason：不支援時要說出為什麼，
    而不是給一顆按不動的按鈕。
    """
    if rule is None:
        return {"supported": False,
                "reason": f"找不到規則 {event.get('rule_id')!r} 的定義。"}
    if not any(f.fp == "src" for f in rule.entity):
        return {"supported": False,
                "reason": f"{rule.name}的對象不含來源 IP"
                          f"（entity：{'、'.join(f.col for f in rule.entity) or '無'}），"
                          f"無法建立來源例外。"}
    # drilldown 已經做完「context 值可不可用」的逐欄位判定（legacy 指紋、
    # 被 scrub 過的值都會被丟掉），直接用它的結果，不要再寫第二套。
    dd = event.get("drilldown") or {}
    ip = (dd.get("filter") or {}).get("source_ip") if dd.get("supported") else None
    if not ip:
        return {"supported": False,
                "reason": "這個事件的來源 IP 無法取得（可能是政策改版前的指紋值）。"}
    return {"supported": True, "source_ip": ip,
            "rule_id": rule.id, "rule_name": rule.name}


def _allowlist_matches(rule, event: dict) -> list[dict]:
    """這個對象目前命中哪些 allowlist 條目，以及為什麼沒生效。

    回答一個保證會被問的問題：「這個 IP 明明在 Allowlist 裡，怎麼還在告警？」
    答案通常是範圍限在別條規則、或條目已到期。
    """
    prefill = event.get("allowlist_prefill") or {}
    ip = prefill.get("source_ip")
    if not ip:
        return []
    out = []
    for row in allowlist.rows(q=ip, limit=20):
        if row["source_ip"] != ip:
            continue
        active = row["id"] in {e.id for e in allowlist.active_entries()}
        scoped_out = row["rule_id"] is not None and row["rule_id"] != event["rule_id"]
        applies = active and not scoped_out
        why = None
        if not applies:
            if scoped_out:
                why = f"範圍限於 {row['rule_id']}，本事件由 {event['rule_id']} 觸發"
            elif row["status"] != allowlist.STATUS_ACTIVE:
                why = f"條目狀態為「{row['status']}」"
            else:
                why = f"條目已於 {row['valid_to']} 到期" if row["valid_to"] else "條目尚未生效"
        out.append({
            "id": row["id"], "name": row["name"],
            "scope": "global" if row["rule_id"] is None else "rule",
            "rule_id": row["rule_id"], "valid_to": row["valid_to"],
            "applies_to_this_rule": applies, "reason_not_applied": why,
        })
    return out


def _build_evidence(row: dict, event: dict) -> dict:
    """證據矩陣：支持攻擊 / 支持正常（設計稿 9.4，皆由實際數據導出）。"""
    attack, normal = [], []
    ctx = event["context"]
    if event["multiple"] and event["multiple"] >= 2:
        attack.append(f"目前值為該對象歷史同時段 median 的 {event['multiple']}× "
                      f"（{event['metric']:.0f} vs {event['median']:.0f}）")
    elif ctx.get("baseline_note"):
        attack.append(f"目前值 {event['metric']:.0f} 超出同時段同類對象的高分位門檻"
                      f"（{event['threshold']:.0f}）")
    if event["threshold"] and not ctx.get("baseline_note"):
        attack.append(f"超過動態門檻 {event['threshold']:.0f}"
                      f"（max(靜態地板, 同時段基線倍數)）")
    if ctx.get("uniq_routes") is not None and ctx["uniq_routes"] <= 3:
        attack.append(f"行為集中於 {ctx['uniq_routes']} 個路由，符合逐筆遍歷特徵")
    if event["brands"] and event["brands"] > 10:
        attack.append(f"涉及 {event['brands']} 個品牌，範圍異常廣{_brand_examples(event)}")
    if event["hit_count"] > 3:
        attack.append(f"連續 {event['hit_count']} 個檢查視窗持續命中")

    if event["p95"] and event["metric"] < event["p95"] * 3:
        normal.append(f"未超過歷史 P95（{event['p95']:.0f}）的三倍，可能為尖峰時段自然波動")
    if event["status"] == "resolved":
        normal.append("事件已回落至基線，未持續擴大")
    if event["hit_count"] <= 2:
        normal.append(f"僅 {event['hit_count']} 個視窗命中，可能為短時批次作業")
    if ctx.get("uniq_routes") and ctx["uniq_routes"] > 8:
        normal.append(f"操作分散於 {ctx['uniq_routes']} 個路由，較接近正常瀏覽行為")
    if event["brands"] == 1:
        only = event["brand_top"][0]["label"] if event["brand_top"] else None
        normal.append(f"僅涉及單一品牌{f'（{only}）' if only else ''}，符合單一整合來源特徵")
    return {"attack": attack, "normal": normal}


def _brand_examples(event: dict) -> str:
    """證據要能自己站得住，所以把最大的幾個品牌寫進句子；完整前十名在上方展開。"""
    listed = brands.top_summary(event["brand_top"])
    return f"，最多的是 {listed}" if listed else ""


def _extra_limitations(event: dict) -> list[str]:
    note = event["context"].get("baseline_note")
    return [note] if note else []


# 事件詳細頁「資料限制」的逐來源說明。**抽成模組常數是為了讓
# tests/test_data_source_coverage.py 看得到** —— 藏在函式內的 local dict
# 沒辦法斷言「每個來源都有一項」。
#
# 這份與 `queries/explorer.source_meta()` 的 `unsupported_filters` 是
# **兩份，刻意不合併**：
#   - 這裡渲染在事件詳細頁，該頁沒有「資料來源」標頭，所以每一句都自帶表名
#     （「API Log 的來源 IP…」）。
#   - explorer.source_meta() 的 unsupported_filters 渲染在 Explorer 該來源
#     「那個篩選欄位旁邊」，表名已在上方（來源下拉本身），再寫一次是噪音。
# 合併之後其中一邊的文案一定會變成謊話（少了主詞，或者多了重複的主詞）。
# 兩份都由覆蓋率測試守著「每個來源都有一項」。
_LIMITATIONS_BY_SOURCE = {
    "admin": ["Admin Log 部分登入紀錄沒有 IP，顯示「來源 IP 不可用」，"
              "此類紀錄無法納入單一來源判斷。"],
    "api": ["API Log 的來源 IP 由 forwarded header 推導，屬「未驗證來源」，"
            "不可作為可信來源證據。",
            "API Log 的 params 大量不是合法 JSON，無法一律展開比對。"],
    "backend": ["Backend System Log 歷史資料可能重複，已以事件 ID 去重後顯示。"],
    "auth": ["Auth Log 為最高敏感等級，僅能提供遮罩摘要。"],
    "order": ["Order Log 沒有 ip 也沒有 headers 欄位，因此完全沒有來源 IP —— "
              "任何「單一來源」的判斷對這張表都不成立。",
              "Order Log 沒有 status／error 欄位，無法區分成功與失敗的操作。",
              "Order Log 的操作者是 _admin，實測全部是 POS 或串接金鑰帳號 —— "
              "它代表哪一支整合程式，不是哪個人。"],
}


def _data_limitations(source_key: str) -> list[str]:
    common = ["目前缺少 device fingerprint，無法確認請求是否來自同一批裝置。",
              "缺少 response bytes 與 row count，因此不可推論「外洩 N 筆資料」。"]
    return _LIMITATIONS_BY_SOURCE.get(source_key, []) + common


# 事件趨勢可以往事件視窗前後各再拉多久。分析師常需要看「事件之前長什麼樣」，
# 只給前後 30 分鐘看不出脈絡。必須與 web/pages/event-detail.js 的 PADS 一致。
EVENT_TREND_PADDINGS = (30, 180, 720, 1440, 2880)


def _event_trend(
    row: dict,
    pad_minutes: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """事件時間序列。

    給 start/end 就用絕對區間，否則以事件視窗前後各 pad_minutes 分鐘。
    分桶依總長自動選，start/end 一律對齊格線（見 trends.resolve_window 的說明）。
    """
    from console.core.ch import query
    from console.queries import exprs, trends
    from console.rules import baseline
    table = settings()["data_sources"].get(row["source_key"], {}).get("table")
    if table is None:
        return {"rows": [], "note": "此規則跨多個資料來源，不提供單一趨勢圖。"}

    if start is None or end is None:
        start = timewin.parse(row["first_seen"]) - timedelta(minutes=pad_minutes)
        end = timewin.parse(row["last_seen"]) + timedelta(minutes=pad_minutes)
    span = max(int((end - start).total_seconds() // 60), 1)
    bucket = trends.bucket_for(span)
    start, end, bucket = trends.resolve_window(start=start, end=end, bucket_minutes=bucket)
    try:
        df = query(
            f"SELECT toStartOfInterval(create_time, INTERVAL {bucket} MINUTE) AS b,"
            f" count() AS cnt FROM {table}"
            f" WHERE {exprs.time_filter()} GROUP BY b ORDER BY b",
            {"start": timewin.fmt(start), "end": timewin.fmt(end)})
    except ChQueryError as exc:
        return {"rows": [], "note": f"趨勢查詢失敗：{exc}"}

    counts = {timewin.fmt(r["b"].to_pydatetime()): int(r["cnt"]) for _, r in df.iterrows()}
    # 零填：沒有零填的話空桶會直接消失，而 category 軸依索引等距排列，
    # 安靜的時段會被壓縮成一段直線而不是凹下去。
    rows = []
    cursor = start
    while cursor < end:
        base = baseline.get(f"table_{bucket}m:{row['source_key']}", hour=cursor.hour,
                            day_class=baseline.day_class_of(cursor))
        rows.append({
            "bucket": cursor.strftime("%m/%d %H:%M"),
            "count": counts.get(timewin.fmt(cursor), 0),
            "median": round(base.median) if base else None,
            "p95": round(base.p95) if base else None,
        })
        cursor += timedelta(minutes=bucket)
    return {
        "rows": rows,
        "pad_minutes": pad_minutes,
        "bucket_minutes": bucket,
        "note": f"此為該資料來源的整體流量趨勢（{bucket} 分鐘分桶），非僅該異常對象的請求量。",
    }


@router.post("/events/{evt_no}/judge")
def judge_event(
    evt_no: str,
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """提交調查判定。

    **判定本身必填，理由／證據／下一步都是選填**（使用者於 2026-08 決定）。
    原本三個都必填的論點是「三個月後最想知道的就是當時為什麼這樣判」，但那個
    要求讓「看過了、確認是誤報」這種一句話就講完的事變成三個輸入框，實際結果是
    大量事件停在**完全沒有判定**的狀態 —— 一個空白的理由欄位仍然留下了「誰在
    什麼時候看過、結論是什麼」，比沒有判定多得多。

    代價是可以留下一個沒有任何理由的判定，所以剩下兩件事變成唯一的約束：
    回應的 note 明說「此判定沒有留下任何理由」，而事件詳細頁會把實際填了什麼
    原樣顯示出來（見 `_judgement_detail`）。**可以不填，但不能安靜。**
    """
    guard(user, "judge_event")
    validate.reject_unknown_keys(payload, {"judgement", *_JUDGEMENT_FIELDS})
    judgement = _require_judgement(payload)
    # 單筆表單三個欄位一律送出，所以這裡是**完整取代**：使用者把某一欄清空
    # 就是要清空它。批次那條路徑相反（留空 = 不動），見 batch_judge_events。
    detail = {k: str(payload.get(k) or "").strip() for k in _JUDGEMENT_FIELDS}
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    with db.tx() as conn:
        _write_judgement(conn, row["id"], judgement, detail, user.email)
    _audit_judgement(user, evt_no, judgement, detail, batch_size=1)
    message = ("本系統不會執行任何自動封鎖、停權或 token 撤銷；"
               "後續處置請於案件中記錄。")
    if not any(detail.values()):
        message += "此判定沒有留下任何理由、證據或處置紀錄。"
    return {"ok": True, "judgement": judgement, "recorded": detail, "note": message}


# 一次可以批次判定幾筆。**與清單的 _EVENT_LIMIT 同一個數字不是巧合**：
# 選取只能從表格那 300 列勾出來（見 web/pages/events.js 的註解），所以送進來
# 比它多就代表呼叫端不是那份清單，那時擋下來比照單全收安全。
_BATCH_JUDGE_LIMIT = _EVENT_LIMIT


def _require_judgement(payload: dict) -> str:
    """判定值必填且是封閉集合的成員。

    「待判定」是 `judgement IS NULL` 的**顯示值**，不是可以提交的判定 ——
    存得進去卻篩不出來的判定值是這裡最容易出現的漂移（見 UNJUDGED 的註解）。
    """
    judgement = payload.get("judgement")
    if judgement not in JUDGEMENTS:
        raise HTTPException(400, f"判定必須是 {'、'.join(JUDGEMENTS)} 之一"
                                 f"（收到 {judgement!r}）")
    return judgement


def _write_judgement(conn, event_id: int, judgement: str, detail: dict,
                     who: str) -> None:
    """把一筆判定寫進 events。單筆與批次共用**同一條寫入路徑**。

    兩個端點各寫一份 UPDATE 的話，遲早有一份漏掉 owner 或把 judgement_note
    存成非 JSON —— 而症狀是「詳細頁讀不回剛剛打的字」，不會有任何錯誤。
    差異只在呼叫端算好的 `detail`（單筆取代、批次合併）。
    """
    # 三個欄位一律寫進 judgement_note，沒填的存成空字串而**不是省略鍵** ——
    # 讀取端才不用去分辨「這次沒填」與「舊資料還沒有這個欄位」。
    note = json.dumps({k: detail.get(k, "") for k in _JUDGEMENT_FIELDS},
                      ensure_ascii=False)
    conn.execute(
        "UPDATE events SET judgement = ?, judgement_note = ?, owner = ? WHERE id = ?",
        (judgement, note, who, event_id))


def _audit_judgement(user: CurrentUser, evt_no: str, judgement: str,
                     detail: dict, *, batch_size: int) -> None:
    """判定的稽核列。**批次一樣是逐筆一列**，只是 target 多一段批次標記。

    一批只寫一列的話，`/api/audit` 用 evt_no 搜尋會搜不到其中 29 筆 ——
    而「每一筆判定都查得到」正是 web/pages/audit-mode.js 對稽查人員的承諾。
    """
    mark = f"（批次 {batch_size} 筆）" if batch_size > 1 else ""
    audit.record(who=user.email, role=user.role_label, action="變更事件狀態",
                 target=f"{evt_no}：判定為 {judgement}{mark}",
                 reason=detail.get("reason") or None)


# 這條路由必須排在任何 `POST /events/{...}` 之前（目前沒有，但加的人會踩到）：
# FastAPI 依宣告順序比對，`/events/{evt_no}` 會把字面值 "judge" 吃掉，
# 而症狀是「批次送出回 404 找不到事件 judge」。同 web/app.js 的 #/rules/R06。
@router.post("/events/judge")
def batch_judge_events(
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """批次提交調查判定（清單頁勾選 N 筆）。

    與單筆端點的三個**刻意的差異**：

    ① **留空的欄位不覆寫既有文字。** 批次送出時三個欄位講的是「這一批共同的
       說法」，而選取裡可能有別人已經判定並寫過證據的事件 —— 一律取代的話那些
       字會靜靜消失。要清空某一欄請進單筆頁面做（那裡的表單顯示的是現值）。
    ② **evt_nos 全部要存在，有一個查不到就整批 400、一列都不寫。** 清單頁送的
       就是它剛剛查到的編號，查不到代表呼叫端送錯了。允許部分成功的話畫面會停在
       「12 筆裡成功 9 筆」而使用者無從知道是哪 3 筆。
    ③ 回應帶 `overwritten` —— 哪幾筆本來就有判定、原本判成什麼。前端在**按下去
       之前**就顯示同一段警告（同 close 的 warnings 做法）。

    判定本身仍然必填、三個文字欄仍然全部選填（2026-08 的決定，見 judge_event）。
    """
    guard(user, "judge_event")
    validate.reject_unknown_keys(payload, {"evt_nos", "judgement", *_JUDGEMENT_FIELDS})
    judgement = _require_judgement(payload)
    evt_nos = _batch_targets(payload)
    rows = db.rows(
        f"SELECT * FROM events WHERE evt_no IN ({','.join('?' * len(evt_nos))})",
        tuple(evt_nos))
    found = {r["evt_no"]: r for r in rows}
    missing = [n for n in evt_nos if n not in found]
    if missing:
        raise HTTPException(
            404, f"找不到這些事件：{'、'.join(missing[:10])}"
                 f"{f' 等 {len(missing)} 筆' if len(missing) > 10 else ''}。"
                 f"沒有任何一筆被判定 —— 請重新整理事件清單再試一次。")
    # 有填的欄位才套用。**空字串與「沒有這個鍵」在這裡是兩件事**，所以用鍵的
    # 有無來表達，不用空字串（見上面 ① 與 _write_judgement 的註解）。
    applied = {k: str(payload.get(k) or "").strip() for k in _JUDGEMENT_FIELDS}
    applied = {k: v for k, v in applied.items() if v}
    overwritten = [{"evt_no": n, "from": found[n]["judgement"]}
                   for n in evt_nos if found[n]["judgement"]]
    with db.tx() as conn:
        for evt_no in evt_nos:
            row = found[evt_no]
            # 逐筆合併：既有的三欄先讀回來，再蓋上這次有填的。
            detail = {**_judgement_detail(row["judgement_note"]), **applied}
            _write_judgement(conn, row["id"], judgement, detail, user.email)
    for evt_no in evt_nos:
        _audit_judgement(user, evt_no, judgement, applied, batch_size=len(evt_nos))
    logger.info("%s 批次判定 %d 筆為 %s（覆寫 %d 筆）",
                user.email, len(evt_nos), judgement, len(overwritten))
    warnings = []
    if overwritten:
        listed = "、".join(f"{o['evt_no']} {o['from']}" for o in overwritten[:5])
        warnings.append(
            f"其中 {len(overwritten)} 筆原本已有判定，已被覆寫（{listed}"
            f"{' 等' if len(overwritten) > 5 else ''}）。舊的判定仍可在操作稽核查到。")
    if not applied:
        warnings.append(
            "這一批判定沒有留下任何理由、證據或處置紀錄 —— 三個欄位皆為選填，"
            "但三個月後想知道「當時為什麼這樣判」時只剩這段文字。")
    return {"ok": True, "judgement": judgement, "count": len(evt_nos),
            "evt_nos": evt_nos, "applied": applied,
            "kept": [k for k in _JUDGEMENT_FIELDS if k not in applied],
            "overwritten": overwritten, "warnings": warnings,
            "note": "本系統不會執行任何自動封鎖、停權或 token 撤銷；"
                    "留空的欄位維持事件原本的內容（要清空請進單筆事件頁）。"}


def _batch_targets(payload: dict) -> list[str]:
    """`evt_nos` → 去重後保持順序的事件編號清單。

    型別一律嚴格檢查：沒有 Pydantic 擋，一個字串（而不是陣列）進來的話
    `for n in evt_nos` 會逐字元跑，症狀是「找不到事件 E」。
    """
    raw = payload.get("evt_nos")
    if not isinstance(raw, list):
        raise HTTPException(400, f"evt_nos 必須是事件編號的陣列"
                                 f"（收到 {type(raw).__name__}）")
    picked: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(400, f"evt_nos 只能是非空字串（收到 {item!r}）")
        value = item.strip()
        if value not in picked:
            picked.append(value)
    if not picked:
        raise HTTPException(400, "沒有選取任何事件 —— 請先在清單勾選要判定的事件")
    if len(picked) > _BATCH_JUDGE_LIMIT:
        raise HTTPException(
            400, f"一次最多判定 {_BATCH_JUDGE_LIMIT} 筆（收到 {len(picked)} 筆）。"
                 f"事件清單一次也只顯示 {_EVENT_LIMIT} 筆，請縮小時間範圍或加上條件。")
    return picked


@router.post("/events/{evt_no}/close")
def close_event(
    evt_no: str,
    payload: dict = Body(default={}),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """人工標為「已處理完畢」（`status = 'closed'`）。

    **必須先有判定。** 沒有判定的結案沒有內容可言，而且會與資安總覽的
    「待判定」橫幅直接矛盾 —— 那條橫幅查的是 `judgement IS NULL`、不看 status，
    所以一筆「已處理完畢但沒有判定」的事件會同時顯示這兩件事。判定現在只要按
    一顆按鈕（理由選填），所以這個前置條件不構成實際負擔。

    **關閉仍在命中的事件是允許的，但會產生後果**：它從「持續中」與資安總覽的
    待處理清單消失（兩處都查 `status = 'active'`），而下一個檢查視窗若仍然命中，
    狀態機找不到 active 列，會建立一個**新的 EVT 編號**（見 store/events.py）。
    回應的 `warnings` 必須把這件事說出來 —— 前端會原樣顯示。
    """
    guard(user, "judge_event")
    validate.reject_unknown_keys(payload or {}, {"reason"})
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    if row["status"] == CLOSED:
        # 回 200 + changed:false 的話畫面會顯示「已標記完成」而什麼都沒發生
        raise HTTPException(409, f"{evt_no} 已經是「已處理完畢」"
                                 f"（{row['closed_by']} 於 {row['closed_at']}）")
    if not row["judgement"]:
        raise HTTPException(
            400, f"{evt_no} 還沒有判定。「已處理完畢」要能回答「處理的結論是什麼」，"
                 f"請先在調查判定送出一個結果（理由、證據、下一步都是選填）。")
    reason = str((payload or {}).get("reason") or "").strip()
    now = timewin.fmt(timewin.taipei_now())
    was_active = row["status"] == "active"
    with db.tx() as conn:
        conn.execute(
            "UPDATE events SET status = ?, closed_at = ?, closed_by = ?, closed_from = ?"
            " WHERE id = ?", (CLOSED, now, user.email, row["status"], row["id"]))
    audit.record(who=user.email, role=user.role_label, action="標為已處理完畢",
                 target=f"{evt_no}：{row['status']} → {CLOSED}", reason=reason or None)
    logger.info("%s 由 %s 標為已處理完畢（原狀態 %s）", evt_no, user.email, row["status"])
    warnings = []
    if was_active:
        warnings.append(
            "這個事件在關閉時仍在持續命中。它會從「持續中」與資安總覽的待處理"
            "清單消失；若下一個檢查視窗仍然命中，系統會另外建立一個新的事件編號 "
            "—— 那不是重複告警，而是它又發生了。")
    return {"ok": True, "status": CLOSED, "closed_at": now, "closed_by": user.email,
            "closed_from": row["status"], "warnings": warnings,
            "note": "監測本身沒有停止 —— 這只是把這一筆從待處理清單移出。"
                    "要讓某個對象不再觸發規則，那是 Allowlist。"}


@router.post("/events/{evt_no}/reopen")
def reopen_event(
    evt_no: str,
    payload: dict = Body(default={}),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """復原人工結案，狀態回到關閉前狀態機的值。

    **一律回 `closed_from`，不可一律回 `active`。** 一筆早就回落的事件被復原成
    active 之後，狀態機會在三個 tick 內把它標 resolved 並對 P0/P1 發一則
    「已恢復」—— 那個事件根本沒有恢復過，它從頭到尾都是靜的。假的「已恢復」
    是這個專案花最多力氣避免的東西（見 store/events._silenced_keys）。
    `closed_from` 是 NULL 的話（理論上不會有：close 一律寫它）退回 resolved，
    那是兩者中不會生出通知的那一個。
    """
    guard(user, "judge_event")
    validate.reject_unknown_keys(payload or {}, {"reason"})
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    if row["status"] != CLOSED:
        raise HTTPException(409, f"{evt_no} 目前不是「已處理完畢」"
                                 f"（狀態為 {row['status']}），沒有可復原的結案")
    # 結案期間同一對象再犯的話已經有一筆新事件了（狀態機找不到 active 列就開新的）。
    # 復原會讓同一個去重鍵有兩筆 active，而狀態機的 db.one 只會拿到其中一筆 ——
    # 另一筆從此不再更新、三個 tick 後被標 resolved。那是靜靜發生的，所以擋在這裡。
    newer = db.one(
        "SELECT evt_no FROM events WHERE rule_id = ? AND entity_key = ?"
        " AND status = 'active' AND id <> ?",
        (row["rule_id"], row["entity_key"], row["id"]))
    if newer:
        raise HTTPException(
            409, f"{evt_no} 結案之後同一對象又觸發了，目前進行中的是 "
                 f"{newer['evt_no']}。復原 {evt_no} 會讓同一個對象有兩筆進行中的"
                 f"事件，而其中一筆會停止更新 —— 請直接處理 {newer['evt_no']}。")
    back_to = row["closed_from"] if row["closed_from"] in ("active", "resolved") else "resolved"
    reason = str((payload or {}).get("reason") or "").strip()
    with db.tx() as conn:
        conn.execute(
            "UPDATE events SET status = ?, closed_at = NULL, closed_by = NULL,"
            " closed_from = NULL WHERE id = ?", (back_to, row["id"]))
    audit.record(who=user.email, role=user.role_label, action="復原事件結案",
                 target=f"{evt_no}：{CLOSED} → {back_to}", reason=reason or None)
    logger.info("%s 由 %s 復原結案（回到 %s）", evt_no, user.email, back_to)
    return {"ok": True, "status": back_to,
            "note": f"狀態回到 {back_to} —— 那是關閉當下狀態機的值，不是重新開始計時。"}


# ─────────────────────────── Log Explorer ───────────────────────────

def _explain_empty(f: explorer.ExplorerFilter) -> dict | None:
    """查詢回 0 筆時，說明是「這個對象不存在」還是「它不在你選的區間」。

    只在有下對象篩選時才問（多一趟查詢，實測回看 365 天約 0.1 秒）。
    沒下篩選就回 0 筆通常是區間太窄或該表沒資料，那個已經看得出來。
    """
    for field, value, label in (("source_ip", f.source_ip, "來源"),
                                ("actor", f.actor, "帳號")):
        if not value:
            continue
        lookback = explorer.extent_lookback_days(f.source, field)
        try:
            extent = explorer.entity_extent(f.source, field, value)
        except ChQueryError:
            # **解釋失敗要說出來，不可以回 None。** 回 None 的話畫面上
            # 「沒有解釋」與「查過了，這個對象真的不存在」長得一模一樣，
            # 而這個函式存在的唯一理由就是分辨這兩件事。
            # 實測觸發路徑：在 API Log 查一個 IP 而結果 0 筆 —— 來源 IP 要對
            # headers 做 JSONExtract，回看查詢會撞上 ClickHouse 的 55 秒上限。
            logger.warning("entity_extent 超時 source=%s field=%s", f.source, field)
            return {
                "kind": "explain_failed", "field": field, "value": value,
                "lookback_days": lookback,
                "message": f"這個區間內沒有資料。**無法進一步確認**這個{label}"
                           f"（{value}）是否存在於其他時間 —— 回看 {lookback} 天的"
                           f"查詢超時了。"
                           + ("API Log 的來源 IP 要逐筆解析 headers，沒有欄位可以剪枝，"
                              "所以這個確認很貴。請把時間區間縮小後重試。"
                              if f.source == "api" and field == "source_ip"
                              else "請把時間區間縮小後重試。"),
            }
        if extent is None:
            continue
        if not extent["found"]:
            return {
                "kind": "not_found", "field": field, "value": value,
                "message": f"回看 {extent['lookback_days']} 天內都找不到這個{label}"
                           f"（{value}）。請確認值是否正確 —— 注意比對是完全相等，"
                           "不是前綴。",
            }
        return {
            "kind": "outside_range", "field": field, "value": value,
            "first_seen": extent["first_seen"], "last_seen": extent["last_seen"],
            "total_in_lookback": extent["count"],
            "message": f"這個{label}（{value}）存在，但活動不在你選的區間內。"
                       f"它在近 {extent['lookback_days']} 天共有 "
                       f"{extent['count']:,} 筆，範圍是 {extent['first_seen']} ~ "
                       f"{extent['last_seen']}。把區間改到這段時間就看得到。",
        }
    return None


@router.get("/explorer/meta")
# 同步 def（同 /explorer 與 /explorer/payload）。這一支不打 ClickHouse，
# 但 `settings()` 有 lru_cache、其餘都是純運算 —— 沒有任何 await，
# 寫成 async def 只會多一個要維護的例外。
def explorer_meta(user: CurrentUser = Depends(current_user)) -> dict:
    """Explorer 的來源清單與每個來源的能力（分析方式、endpoint 欄位標籤、
    不支援的篩選及其原因）。

    **刻意是獨立的 GET，不塞進 `POST /explorer` 的回應。** 這份資料每次查詢
    都一樣，塞進去等於每次查詢都白傳一份；而前端只在 mounted 時要一次。

    **刻意不含逐來源的「資料限制」清單。** 渲染它的那一欄（Explorer 最右側的
    「欄位說明與資料限制」）已於 2026-08-07 移除；那些資訊分散在
    `queries/health._NOTES`（健康卡）、`_LIMITATIONS_BY_SOURCE`（事件詳細頁）
    與這裡的 `unsupported_filters`（顯示在被擋住的那個欄位旁邊）。
    `tests/test_explorer_source_meta.py::test_meta_does_not_ship_a_field_nobody_renders`
    反向守著「不要加回沒有消費端的欄位」。
    """
    guard(user, "use_explorer")
    return {"sources": explorer.source_meta()}


@router.post("/explorer")
# 刻意用同步 def，不是 async def（同 /sweep 與 /explorer/payload）。
#
# 裡面的 ClickHouse 查詢是**阻塞**的。寫成 async def 時它跑在事件迴圈上，
# 一個慢查詢會讓**整個主控台**停止回應 —— 實測在 API Log 查一個 IP（來源 IP 要
# 解析 headers、回看查詢跑滿 55 秒）期間，完全不碰 ClickHouse 的 `/api/session`
# 被拖到 53.6 秒，五分鐘排程也一起卡住。
# 使用者看到的症狀不是「這個查詢很慢」，而是「篩選、Controller 建議、全部功能
# 都壞掉了」—— 因為那段時間所有請求都排在後面。
def run_explorer(
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "use_explorer")
    analysis = payload.get("analysis", "trend")
    # brand 一律轉 int：事件 context 存的是 float（`4748.0` —— pandas 把只有數值
    # 欄位的整列升成 float64），直接送下去會流進 `_brand = %(brand)s` 與
    # `brands.label()`，畫面上就會看到 `4748.0` 而不是品牌名稱。給了值但解不出
    # 整數時明確回 400，不要靜靜把篩選條件丟掉 —— 那會回一份「全品牌」的結果。
    brand = brands.coerce_id(payload.get("brand"))
    if payload.get("brand") not in (None, "") and brand is None:
        raise HTTPException(400, f"品牌編號 {payload.get('brand')!r} 不是整數")
    # 分店同理（`stores.coerce_id` 就是 `brands.coerce_id`）。事件 context 存的是
    # float（pandas 把純數值列升成 float64），`27681.0` 直送會流進 `_store = %(store)s`
    # 而命中 0 筆 —— 畫面上是「這個分店在這段時間沒有活動」，完全看不出是型別問題。
    store = stores.coerce_id(payload.get("store"))
    if payload.get("store") not in (None, "") and store is None:
        raise HTTPException(400, f"分店編號 {payload.get('store')!r} 不是整數")
    f = explorer.ExplorerFilter(
        source=payload.get("source", "api"),
        start=payload.get("start", ""), end=payload.get("end", ""),
        brand=brand, store=store, endpoint=payload.get("endpoint"),
        # 依對象反查：把掃描結果或排名裡看到的帳號／IP 貼回來追明細
        source_ip=payload.get("source_ip"), actor=payload.get("actor"),
        only_error=bool(payload.get("only_error")),
        limit=int(payload.get("limit", 500)),
    )
    started = time.time()
    try:
        if analysis == "trend":
            data = explorer.trend(f, payload.get("bucket", "10m"))
        elif analysis in ("endpoint", "brand", "source", "actor"):
            data = explorer.ranking(f, analysis)
        elif analysis == "error":
            data = explorer.error_analysis(f)
        elif analysis == "unique_resource":
            data = explorer.unique_resource(f)
        elif analysis == "detail":
            guard(user, "view_masked_detail")
            data = explorer.detail(f)
        else:
            raise HTTPException(400, f"未知分析方式 {analysis!r}")
    except explorer.FilterError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)
    # 「0 筆」要自己解釋原因。使用者從掃描結果貼一個對象進來，Explorer 的預設區間
    # 比掃描短得多（實測 192.168.97.1 最後出現在 7/29，而預設是最近 1 小時），
    # 只顯示空表格會讓人以為「這個對象不存在」。
    # 有 `total` 的分析（趨勢、排名、明細、unique）一律以它為準，不看 rows ——
    # 趨勢的 rows 是**零填**的（見 explorer.trend），永遠非空，用 rows 判斷等於
    # 讓「0 筆」的解釋從趨勢圖上永遠消失，而畫面上是一整排 0：那正是
    # 「把沒有資料渲染成沒有發生」。沒有 total 的（error_analysis）才退回看 rows。
    empty = data["total"] == 0 if "total" in data else not data.get("rows")
    if empty:
        data["empty_reason"] = _explain_empty(f)
    rng = f"{f.start} ~ {f.end}"
    qh = audit.record(who=user.email, role=user.role_label, action="Log Explorer 查詢",
                      target=f"{f.source}/{analysis}", query_text=json.dumps(payload, sort_keys=True),
                      time_range=rng, row_count=data.get("returned") or data.get("total"),
                      duration_ms=elapsed)
    return {**data, "meta": {
        "elapsed_ms": elapsed, "time_range": rng, "query_hash": qh,
        "brand_filter": brands.label(f.brand) if f.brand is not None else None,
        "store_filter": stores.label(f.store) if f.store is not None else None,
        "dedup": "以事件 ID（_id）去重", "timezone": "Asia/Taipei",
        "data_latest": health.freshness_summary(health.source_health())["latest"],
    }}


@router.get("/brands")
def search_brands(
    q: str = "",
    limit: int = Query(brand_search.DEFAULT_LIMIT, ge=1, le=brand_search.MAX_LIMIT),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """品牌選擇器的候選清單（Log Explorer 的品牌篩選欄位）。

    不記稽查：品牌名稱是營運資訊而非個資，而 debounce 打字會把 audit_log 洗版，
    稀釋真正該追的「Log Explorer 查詢」。實際的查詢行為在 POST /explorer 已記錄，
    meta.brand_filter 也會帶出最後選了哪個品牌。
    """
    guard(user, "use_explorer")
    try:
        return {"rows": brand_search.search(q, limit)}
    except ChQueryError as exc:
        # 不吞成空陣列 —— 空陣列在 UI 上等於「查無此品牌」，與查詢失敗是兩回事。
        # 同 core/brands.py 的「（查無品牌）」vs「（品牌名稱查詢失敗）」之分。
        raise HTTPException(502, f"品牌查詢失敗：{exc}") from exc


@router.get("/stores")
def search_stores(
    q: str = "",
    brand: str = "",
    limit: int = Query(store_search.DEFAULT_LIMIT, ge=1, le=store_search.MAX_LIMIT),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """分店選擇器的候選清單（Log Explorer 的分店篩選欄位）。

    `brand` 有值時硬性限定在該品牌之下 —— Explorer 的分店欄位連動上面的品牌
    選擇器（見 queries/store_search.py 的模組說明）。不記稽查，理由同 /api/brands。

    `brand` 刻意宣告成 `str` 而不是 `int | None`：宣告成 int 的話 FastAPI 對
    空字串會回 422（前端沒選品牌時送的就是空字串），而那個 422 在選單裡看起來
    是「查詢失敗」。這裡自己解析：空 = 不限，解不出整數 = 400。
    """
    guard(user, "use_explorer")
    brand_id = stores.coerce_id(brand) if brand.strip() else None
    if brand.strip() and brand_id is None:
        raise HTTPException(400, f"品牌編號 {brand!r} 不是整數")
    try:
        rows = store_search.search(q, brand=brand_id, limit=limit)
        # 沒被截斷就不必再查一趟 —— 分店選擇器打字時是 debounce 逐次呼叫的。
        total = len(rows) if len(rows) < limit else store_search.count(q, brand=brand_id)
        return {"rows": rows, "total": total}
    except ChQueryError as exc:
        # 不吞成空陣列 —— 空陣列在 UI 上等於「查無此分店」，與查詢失敗是兩回事。
        raise HTTPException(502, f"分店查詢失敗：{exc}") from exc


@router.get("/endpoints")
def suggest_endpoints(
    source: str = "api",
    start: str = "",
    end: str = "",
    user: CurrentUser = Depends(current_user),
) -> dict:
    """該區間內的 endpoint 候選值，依呼叫量由高到低（Log Explorer 的建議選單）。

    一次回傳全部而非分頁：基數有界且小（實測最多約 600 種），前端過濾才能
    做到零延遲且完整 —— 只取 top N 會讓罕見的 endpoint 永遠找不到。
    不記稽查，理由同 GET /api/brands。
    """
    guard(user, "use_explorer")
    try:
        return endpoint_suggest.suggest(source, start, end)
    except explorer.FilterError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"endpoint 查詢失敗：{exc}") from exc


# ─────────────────────────── 快速查詢 ───────────────────────────

@router.get("/quick")
def quick_catalog(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_quick")
    return {"categories": quick_templates.catalog()}


@router.post("/quick/{template_id}")
def quick_run(
    template_id: str,
    payload: dict = Body(default={}),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_quick")
    tpl = quick_templates.BY_ID.get(template_id)
    if tpl is None:
        raise HTTPException(404, f"找不到模板 {template_id}")
    started = time.time()
    try:
        data = tpl.run(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)
    qh = audit.record(who=user.email, role=user.role_label, action="執行模板",
                      target=tpl.name, query_text=f"{template_id}:{json.dumps(payload, sort_keys=True)}",
                      time_range=data.get("time_range"), row_count=len(data.get("rows", [])),
                      duration_ms=elapsed)
    return {**data, "template": {"id": tpl.id, "name": tpl.name, "source": tpl.source},
            "meta": {"elapsed_ms": elapsed, "query_hash": qh}}


# ─────────────────────────── 資料健康 ───────────────────────────

@router.get("/health")
def data_health(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_health")
    cards = health.source_health()
    hb = db.one("SELECT * FROM heartbeat WHERE key = 'five_min'")
    daily = db.one("SELECT * FROM heartbeat WHERE key = 'daily'")
    return {
        "sources": cards,
        "freshness": health.freshness_summary(cards),
        "thresholds": settings()["freshness"],
        "heartbeat": {"five_min": hb, "daily": daily},
    }


@router.get("/sparklines")
def data_sparklines(user: CurrentUser = Depends(current_user)) -> dict:
    """統計卡的迷你趨勢線。刻意獨立於 /health —— source_health() 有三個呼叫端，
    只有一個要這份資料（詳見 queries/sparklines.py 的模組說明）。"""
    guard(user, "view_health")
    return sparklines.source_sparklines()


# 逐筆調閱完整 payload。
#
# 一般明細的 params 只給大小與欄位名稱（見 core/masking.payload_summary），因為
# 那些內容混著 authorization／cookie／secret，以及消費者的手機與 Email，而畫面、
# Slack、磁碟上的 log 都會沾到。但「要查的時候查不到」也不行 —— 所以保留這條路徑。
#
# 收斂的方式是「一次一筆、寫入 audit_log」，**不要求填理由**：這是對內的調查工具，
# 每次調閱都要打字說明只會讓人繞過它去直接查 DB，反而失去留痕。
# 稽核仍然記下誰、何時、哪一筆。
@router.post("/explorer/payload")
def explorer_payload(
    payload_body: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_raw_payload")
    source = str(payload_body.get("source") or "")
    row_id = str(payload_body.get("row_id") or "")
    try:
        data = explorer.payload(source, row_id)
    except explorer.FilterError as exc:
        # 失敗也要留痕：查不到什麼也是稽核事實
        audit.record(who=user.email, role=user.role_label, action="調閱完整 payload",
                     target=f"{source}/{row_id}", result="失敗", reason=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc

    audit.record(who=user.email, role=user.role_label, action="調閱完整 payload",
                 target=f"{source}/{row_id}", time_range=data.get("time"),
                 row_count=1)
    return data


# ─────────────────────────── 期間異常掃描 ───────────────────────────

# 掃描的區間上限與 Explorer 的 MAX_CUSTOM_RANGE_DAYS 刻意不同：Explorer 的限制來自
# 「排名要對 api_log 的 headers 做 JSONExtract」，而掃描的低成本探針全部走
# backend/admin/auth，實測 94 天的完整掃描牆鐘 1.9 秒。對齊稽查匯出的上限。
SWEEP_MAX_RANGE_DAYS = 92


def _sweep_window(payload: dict) -> tuple[datetime, datetime]:
    start_s, end_s = payload.get("start"), payload.get("end")
    if not start_s or not end_s:
        raise HTTPException(400, "必須指定 start 與 end（格式 YYYY-MM-DD[ HH:MM:SS]）")
    try:
        start, end = timewin.parse(start_s), timewin.parse(end_s)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if start >= end:
        raise HTTPException(400, "開始時間必須早於結束時間")
    span_days = (end - start).total_seconds() / 86400
    if span_days > SWEEP_MAX_RANGE_DAYS:
        raise HTTPException(
            400, f"掃描區間最長 {SWEEP_MAX_RANGE_DAYS} 天，目前為 {span_days:.1f} 天")
    return start, end


# 刻意用同步 def，不是 async def。裡面的 ClickHouse 查詢是阻塞的，寫成 async def
# 會佔住事件迴圈 —— 勾了 API 來源探針時單次要 30 秒，整台 server（含五分鐘檢查
# 排程與其他人的請求）都會卡住。同步的 path operation 由 FastAPI 丟進 threadpool。
@router.post("/sweep")
def run_sweep(
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "run_sweep")
    start, end = _sweep_window(payload)
    include_api = bool(payload.get("include_api_probe"))
    started = time.time()
    try:
        result = sweep_report.build(start, end, include_high_cost=include_api,
                                    intel_available=intel_store.available())
    except ChConnectionError as exc:
        raise HTTPException(503, f"ClickHouse 連線失敗：{exc}") from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)

    sweep_no = sweeps.save(result, created_by=user.email, duration_ms=elapsed,
                           include_api_probe=include_api)
    audit.record(who=user.email, role=user.role_label, action="執行期間異常掃描",
                 target=sweep_no,
                 query_text=json.dumps({"start": timewin.fmt(start),
                                        "end": timewin.fmt(end),
                                        "include_api_probe": include_api},
                                       sort_keys=True),
                 time_range=f"{timewin.fmt(start)} ~ {timewin.fmt(end)}",
                 row_count=result["summary"]["findings"], duration_ms=elapsed)
    return {**result, "sweep_no": sweep_no, "duration_ms": elapsed,
            "include_api_probe": include_api}


@router.get("/sweep")
def list_sweeps(
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "run_sweep")
    return {"sweeps": sweeps.recent(limit),
            "max_range_days": SWEEP_MAX_RANGE_DAYS,
            "intel_available": intel_store.available()}


@router.get("/sweep/{sweep_no}")
def get_sweep(sweep_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "run_sweep")
    stored = sweeps.load(sweep_no)
    if stored is None:
        raise HTTPException(404, f"找不到掃描 {sweep_no}")
    audit.record(who=user.email, role=user.role_label, action="查看期間異常掃描",
                 target=sweep_no, row_count=len(stored["findings"]))
    return stored


# 敘事是獨立端點，不併進 /sweep：確定性的結果要能先上畫面，LLM 慢或掛掉
# 都不該讓整份報告產不出來。
@router.post("/sweep/{sweep_no}/narrate")
def narrate_sweep(sweep_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "run_sweep")
    stored = sweeps.load(sweep_no)
    if stored is None:
        raise HTTPException(404, f"找不到掃描 {sweep_no}")
    started = time.time()
    result = narrate.write(stored)
    elapsed = int((time.time() - started) * 1000)
    if result.markdown:
        sweeps.save_narrative(sweep_no, result.markdown, result.model)
    audit.record(who=user.email, role=user.role_label, action="產生 AI 研判草稿",
                 target=sweep_no, duration_ms=elapsed,
                 result="成功" if result.markdown else "失敗",
                 reason=result.error)
    return {
        "sweep_no": sweep_no,
        "markdown": result.markdown,
        "model": result.model,
        "error": result.error,
        "disclaimer": narrate.DISCLAIMER,
    }


# 規則端點已移到 api/rules_routes.py（URL 不變）。
# 那裡多了 PATCH 與覆寫，塞在這個檔案會讓它破千行。
