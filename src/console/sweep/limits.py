"""自動產生「資料範圍與可信度限制」。

這是報告第二節的等價物，而且**可以完全自動化** —— 每一條都是查得到的事實或
設定裡就有的值，不需要人工判斷。

為什麼它和事件清單一樣重要：掃描結果會被當成比它實際更確定的東西。
「這個帳號沒有異常」和「這段期間根本沒記 IP，所以查不到來源」是完全不同的結論，
但畫面上長得一樣。少了這一段，讀者無法分辨「沒找到」與「找不到」。

回傳的每一條都有 `level`：
    info     只是要讓讀者知道的範圍界定
    caution  會影響某類結論的強度
    blocking 這段期間的某類判讀根本不成立
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from console.core import timewin
from console.core.ch import ChQueryError, query
from console.core.config import settings
from console.intel import ranges
from console.intel import store as intel_store
from console.queries import exprs
from console.sweep.probes import by_id
from console.sweep.run import ProbeRun

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limitation:
    key: str
    title: str
    detail: str
    level: str          # info | caution | blocking


def _coverage(start: datetime, end: datetime) -> dict:
    """區間內 backend log 的 IP 記錄涵蓋率與多段 XFF 筆數（一趟查詢）。"""
    sql = f"""
    SELECT count() AS total,
           countIf(ip IS NULL OR ip = '') AS no_ip,
           countIf(position(coalesce(ip, ''), ',') > 0) AS multi_hop,
           uniqExact(coalesce(ip, '')) AS uniq_ips
    FROM ods_backend_sys_log
    WHERE {exprs.time_filter()}
    """
    row = query(sql, {"start": timewin.fmt(start), "end": timewin.fmt(end)}).iloc[0]
    return {k: int(row[k]) for k in ("total", "no_ip", "multi_hop", "uniq_ips")}


def _auth_actions(start: datetime, end: datetime) -> list[str]:
    sql = f"""
    SELECT action FROM ods_auth_log
    WHERE {exprs.time_filter()} GROUP BY action ORDER BY count() DESC LIMIT 5
    """
    df = query(sql, {"start": timewin.fmt(start), "end": timewin.fmt(end)})
    return [str(a) for a in df["action"]]


def _overlapping_exclusions(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """區間是否與已知事件污染窗重疊。

    重要：這些窗是**基線計算**要排除的區間。掃描本身不排除它們（使用者就是要查
    那段），但區間之前的基線若落在污染窗內，比較的基準會被拉高、倍數被低估。
    """
    out = []
    for s, e in settings()["baseline"].get("exclusion_windows", []):
        ws, we = timewin.parse(s), timewin.parse(e)
        if ws < end and we > start:
            out.append((s, e))
    return out


def collect(start: datetime, end: datetime, probe_run: ProbeRun) -> list[Limitation]:
    """組出這次掃描的限制清單。查詢失敗不擋報告 —— 改為註明「無法確認」。"""
    items: list[Limitation] = []
    cfg = settings()

    # ── 區間界定 ──
    lag = cfg["time"]["lag_buffer_minutes"]
    items.append(Limitation(
        key="range",
        title="掃描區間",
        detail=f"{timewin.fmt(start)} ~ {timewin.fmt(end)}（台北牆鐘，共 "
               f"{(end - start).total_seconds() / 86400:.1f} 天）。資料自 Mongo 同步進 "
               f"ClickHouse 約有落地延遲，右界前 {lag} 分鐘內的資料可能尚未齊全。",
        level="info"))

    # ── IP 涵蓋率：決定所有「來源」結論是否成立 ──
    try:
        cov = _coverage(start, end)
    except (ChQueryError, IndexError) as exc:
        logger.warning("涵蓋率查詢失敗：%s", exc)
        items.append(Limitation(
            key="ip_coverage", title="IP 記錄涵蓋率",
            detail="無法確認（查詢失敗），所有以來源為對象的結論請自行驗證。",
            level="caution"))
    else:
        total, no_ip = cov["total"], cov["no_ip"]
        miss = (no_ip / total) if total else 0.0
        if total == 0:
            items.append(Limitation(
                key="ip_coverage", title="區間內無後台紀錄",
                detail="這段期間 ods_backend_sys_log 沒有任何資料，後台相關的探針"
                       "全部無從判斷。請確認區間是否落在資料範圍內。",
                level="blocking"))
        elif miss >= 0.2:
            items.append(Limitation(
                key="ip_coverage", title="IP 記錄大量缺漏",
                detail=f"區間內 {no_ip:,} / {total:,} 筆（{miss:.1%}）沒有來源 IP。"
                       "IP 欄位在 2026-04 中旬才啟用，此前的異常無法追溯來源 —— "
                       "所有以來源為對象的判讀（憑證集中、首見來源、來源型態）"
                       "在缺漏的部分完全不成立，不是「沒有異常」。",
                level="blocking"))
        elif no_ip:
            items.append(Limitation(
                key="ip_coverage", title="IP 記錄少量缺漏",
                detail=f"區間內 {no_ip:,} / {total:,} 筆（{miss:.2%}）沒有來源 IP，"
                       "這部分的來源歸因不成立。",
                level="caution"))

        if cov["multi_hop"]:
            items.append(Limitation(
                key="xff_not_normalised", title="X-Forwarded-For 未正規化",
                # 這裡刻意不舉實際的 IP 當例子。原始 IP 不得離開 process ——
                # 連「說明文字裡的示範值」也不行（tests/test_masking_audit.py 會擋）。
                detail=f"區間內 {cov['multi_hop']:,} 筆的 IP 欄位是逗號分隔的多段字串"
                       "（客戶端 IP 加上一或多個代理跳點），系統直接寫入原始標頭。"
                       "多數為 Zscaler 企業代理，屬正常網路行為，但這既造成來源統計失真，"
                       "也代表應用層信任了客戶端可控的標頭。**這是資料品質問題，"
                       "已刻意不列為事件**；首段為私有位址的偽造案例才會出現在清單中。",
                level="caution"))

    # ── 認證日誌沒有成敗欄位：暴力破解無從偵測 ──
    try:
        actions = _auth_actions(start, end)
    except ChQueryError as exc:
        logger.warning("auth action 查詢失敗：%s", exc)
    else:
        if len(actions) <= 1:
            only = actions[0] if actions else "（無資料）"
            items.append(Limitation(
                key="auth_no_outcome", title="認證日誌無成敗欄位",
                detail=f"ods_auth_log 的 action 在這段期間僅有單一值「{only}」，"
                       "無法區分認證成功與失敗。因此高次數認證只能解讀為"
                       "「反覆取得憑證」，**不能**推論為暴力破解。"
                       "後台登入的成敗另由 ods_admin_log 記錄（登入失敗集中探針走那張表）。",
                level="caution"))

    # ── 基線污染窗 ──
    overlaps = _overlapping_exclusions(start, end)
    if overlaps:
        windows = "、".join(f"{s} ~ {e}" for s, e in overlaps)
        items.append(Limitation(
            key="exclusion_overlap", title="區間與已知事件窗重疊",
            detail=f"掃描區間與設定中的已知污染窗重疊（{windows}）。掃描本身不排除"
                   "這些時段（要查的就是它們），但若同一個對象的「區間之前」基線也落在"
                   "污染窗內，比較基準會被墊高、倍數被低估。",
            level="caution"))

    # ── 來源情報涵蓋率 ──
    cov = intel_store.coverage()
    if not cov["total"]:
        items.append(Limitation(
            key="intel_missing", title="來源型態未涵蓋",
            detail="來源情報（ip_intel）是空的，因此「來源為機房／VPN」與"
                   "「同帳號機房與一般來源並存」兩支探針未執行。報告中最強的"
                   "單一訊號（真人不會從資料中心登入後台）在本次掃描中**等於沒有檢查**。"
                   "執行 `uv run python -m console.intel.refresh` 建立。",
            level="blocking"))
    else:
        items.append(Limitation(
            key="intel_coverage", title="來源型態的判定範圍",
            detail=f"已分類 {cov['total']:,} 個來源，其中機房 {cov['hosting']:,} 個、"
                   f"VPN／匿名代理 {cov['vpn']:,} 個。分類來自離線的雲端業者公開範圍檔"
                   f"（快照 {ranges.SNAPSHOT}）與人工清單，**未涵蓋的來源標為「歸屬未知」"
                   "而不是「住宅寬頻」** —— 「查不到歸屬」不等於「不是機房」。"
                   "小型 VPS 商與新啟用的網段特別容易落在未涵蓋的部分。",
            level="caution"))

    # ── 敏感路由清單：空的等於這項檢查沒有執行 ──
    #
    # 空清單影響的不只「敏感路由大量存取」（P03）本身：P02（集中存取資料導出路由）
    # 也吃同一份清單，而 P02 是 concentration 這組**唯一**的訊號來源，也是唯一真正
    # 與量級無關的訊號（P01/P03 同屬 volume，量級突變已經有 P01 頂著，不會因為
    # P03 沒跑就少一組）。correlate.py 要求交叉命中兩組獨立訊號才會列入報告，
    # 少了 concentration 之後，一個帳號只剩 volume 這一票，第二票只能靠
    # source_type（來源是機房／VPN）或 off_hours（非上班時間）幫忙湊。
    # 於是一個來源正常、白天上班時間、但高度集中打某個資料導出路由的帳號，
    # 永遠湊不到第二組訊號 —— **完全不會出現在報告裡**，而這正是比毫無掩飾的
    # 攻擊者更謹慎的那種行為模式。
    routes = exprs.sensitive_routes()
    if not routes:
        items.append(Limitation(
            key="sensitive_routes_empty",
            title="敏感路由清單是空的",
            detail="「集中存取資料導出路由」與「敏感路由大量存取」兩支探針都"
                   "**沒有執行**。清單目前一條生效中的路由都沒有（可在規則頁面"
                   "編輯）。前者是與量級無關的 concentration 訊號唯一的來源 ——"
                   "少了它，一個帳號的第二組訊號只能靠來源型態或非上班時間湊，"
                   "在上班時間、來源看起來正常的情況下高度集中存取資料導出路由"
                   "**完全不會被列入報告**。這不是「沒有異常」，是沒有檢查。",
            level="blocking"))
    else:
        items.append(Limitation(
            key="sensitive_routes",
            title="敏感路由的範圍",
            detail=f"「集中存取資料導出路由」與「敏感路由大量存取」兩支探針都只"
                   f"涵蓋清單上的 {len(routes)} 條路由（{'、'.join(routes)}）。"
                   "清單之外的路由由「帳號自身量級突變」涵蓋量的面向，但不會被"
                   "算進這兩支探針的訊號。清單可在規則頁面編輯，改動同時影響"
                   "即時規則 R05。",
            level="info"))

    # ── 探針覆蓋：跳過與失敗都必須說出來 ──
    if probe_run.skipped:
        names = "、".join(
            f"{pid}（{p.name}）" for pid in probe_run.skipped if (p := by_id(pid)))
        items.append(Limitation(
            key="probes_skipped", title="部分探針未執行",
            detail=f"未執行：{names}。其中 API 來源分析需另外勾選（3.4 億列解 headers "
                   "JSON，90 天約 30 秒）；需要來源情報的探針在 ip_intel 尚未建立時"
                   "自動跳過。這些訊號在本次掃描中等於沒有檢查。",
            level="caution"))
    if probe_run.failures:
        detail = "；".join(f"{pid}: {err}" for pid, err in probe_run.failures.items())
        items.append(Limitation(
            key="probes_failed", title="部分探針執行失敗",
            detail=f"{detail}。這些訊號本次沒有檢查到，清單可能不完整。",
            level="blocking"))

    # ── API 來源的先天限制 ──
    if "P13" not in probe_run.skipped:
        items.append(Limitation(
            key="api_src_unverified", title="API 來源屬未驗證來源",
            detail="ods_api_log 沒有獨立的來源欄位，API 來源 IP 是從 headers 的 "
                   "X-real-ip / X-forwarded-for 推導而來。這些標頭由客戶端送出、"
                   "可任意偽造，除非後端只信任自家代理鏈的最後一跳。",
            level="caution"))

    items.append(Limitation(
        key="masking", title="對象一律為不可還原的 fingerprint",
        detail="清單中的對象是 HMAC 指紋（actor_/src_），系統沒有還原成原始帳號或 IP "
               "的功能。要對應到真實對象需另循權限程序，並以 Explorer 用同一個 "
               "fingerprint 反查明細。",
        level="info"))

    return items
