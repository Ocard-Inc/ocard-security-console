"""例外名單（allowlist）的唯一讀寫入口。

這張表是**整個系統唯一的靜音開關**：即時規則引擎（rules/engine.py）與期間掃描
（sweep/report.py）都讀它，命中就整筆丟棄 —— 不產生 event、不進通知。
所以它值得一個單一入口，而不是四個地方各寫一份 SQL。

三件散開來就會壞掉的事：

**status 的字面值是隱性契約。** 讀取端只認 `'生效中'`。寫入 `'啟用'` 或
`'active'` 的話，畫面顯示條目在生效，而引擎與掃描**完全看不到它** ——
沒有任何錯誤訊息。所以字面值只存在這個模組的常數裡。

**範圍語意。** `rule_id IS NULL` = 全域（所有規則 + 期間掃描）；有值 = 只對該
規則生效，**不影響掃描**（掃描不是規則驅動的，套上去沒有意義）。
`endpoint` 非空時再限縮到那個端點，與 rule_id 的關係是 AND。

**規則範圍的條目可以沒有 source_ip。** 實測 `Api2/GetProfile` 的大量呼叫同時被
兩條規則抓到：R03（entity = src + endpoint，例如 `18.182.228.100 · Api2/GetProfile`）
與 R04（entity 只有 endpoint，`Api2/GetProfile`）。R04 的對象**根本沒有來源 IP**，
所以「IP + endpoint」的例外只能讓 R03 閉嘴，R04 會繼續叫 —— 那等於沒解決問題。
因此規則範圍的條目只要有 `source_ip` 或 `endpoint` 其中一個就成立。
**全域條目仍然必須有 IP**：全域 + 只有 endpoint 等於「17 條規則都不看這個端點」，
那個盲區太大而且沒有已知用途。
兩者都沒有的話等於「這條規則永不觸發」—— 那應該去停用規則（停用會出現在
資安總覽的橫幅上，靜靜掛一筆空例外不會）。

**valid_from / valid_to 是字串比較。** SQL 兩邊都是 `YYYY-MM-DD HH:MM:SS`，
而原生 `<input type="datetime-local">` 給的是 `2026-08-03T00:00`。
`'T'`(0x54) > `' '`(0x20)，所以 `'2026-08-03T00:00' <= '2026-08-03 12:00:00'`
是 **False** —— 條目永遠不生效，而畫面顯示「生效中」。反過來帶 T 的 valid_to
則永不過期。只給日期的 valid_to 更陰險：在到期日當天 `'2026-12-31' >=
'2026-12-31 09:00:00'` 為 False，**最後一天整天提早失效**。
一律經 normalize_bound() 正規化，不接受未經處理的字串。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from console.core import timewin
from console.store import db

STATUS_ACTIVE = "生效中"
STATUS_DISABLED = "已停用"
STATUS_PENDING = "待核准"          # schema 的 DEFAULT；新 API 不會產生這種列
STATUSES = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_PENDING)

# 比對只需要這幾個欄位。完整的一列由 rows()/get() 提供給 API。
_MATCH_COLS = ("id, name, source_ip, COALESCE(endpoint, '') AS endpoint,"
               " rule_id, valid_from, valid_to")


@dataclass(frozen=True)
class Entry:
    id: int
    name: str
    source_ip: str           # '' = 不限來源（只有規則範圍 + endpoint 的條目才可能）
    endpoint: str            # '' = 全部端點
    rule_id: str | None      # None = 全域
    valid_from: str | None
    valid_to: str | None


@dataclass(frozen=True)
class Index:
    """比對用的索引。engine 每個 tick 建一次。"""
    by_ip: dict[str, tuple[Entry, ...]]
    # 沒有 source_ip、只靠 endpoint 縮小的條目，以 rule_id 為鍵。
    # 這些條目沒有 IP 可以當索引鍵，所以要另外一張表。
    by_rule: dict[str, tuple[Entry, ...]]


# 「這張表還能為這條規則再縮小到什麼」。鍵是 allowlist 的欄位名，
# entity_cols 是規則 entity 裡代表那個維度的欄位（`fp: null` 的原樣欄位）。
#
# 目前只有 endpoint。加新維度（例如品牌，`brand_scope` 欄位已存在但零讀者）
# 就是在這裡加一筆 + 在 match() 比對它 —— **但不要先加沒有讀者的維度**，
# 那會產生一個「存了看起來生效、實際完全沒作用」的輸入框。
_DIMENSIONS = (
    {"key": "endpoint", "entity_cols": ("endpoint", "route2")},
)


def dimensions(rule) -> tuple[str, ...]:
    """這條規則可以再用哪些 allowlist 欄位縮小範圍。"""
    cols = {f.col for f in rule.entity if f.fp is None}
    return tuple(d["key"] for d in _DIMENSIONS
                 if cols & set(d["entity_cols"]))


def has_source(rule) -> bool:
    """規則的對象含來源 IP 嗎（決定 IP 欄位是必填還是選填）。"""
    return any(f.fp == "src" for f in rule.entity)


def allowlistable(rule) -> bool:
    """這條規則能不能被 allowlist 抑制。

    有來源 IP 或有可縮小的維度都算 —— R04 這種只有 endpoint 的規則以前被判成
    「不適用」，而它正是 GetProfile 大量呼叫最主要的告警來源。
    """
    return has_source(rule) or bool(dimensions(rule))


# ─────────────────────────── 時間正規化 ───────────────────────────

def normalize_bound(value: str, *, end_of_day: bool) -> str:
    """使用者輸入 → 可安全比較的台北牆鐘字串。解析不了就拋 ValueError。

    `end_of_day`（valid_to 用）會把「只給日期」補成 23:59:59。少了這一步，
    到期日當天整天都算已過期 —— 早一整天失效，且完全沒有徵兆。
    """
    text = str(value).strip()
    dt = timewin.parse(text)                 # 帶 'T' 的值在這裡就會被拒絕
    date_only = " " not in text and ":" not in text
    if end_of_day and date_only:
        dt = dt.replace(hour=23, minute=59, second=59)
    return timewin.fmt(dt)


# ─────────────────────────── 讀取與比對 ───────────────────────────

def active_entries(*, now: str | None = None) -> tuple[Entry, ...]:
    """生效中且在有效期內的條目（含全域與規則範圍）。

    至少要有 source_ip 或 endpoint 其中一個 —— 兩者都空的條目不會比對到任何
    東西（也不該存在，寫入端會擋），把它們留在集合裡只會讓「生效中 N 筆」變成
    一個沒有意義的數字。
    """
    at = now or timewin.fmt(timewin.taipei_now())
    return tuple(
        Entry(r["id"], r["name"], r["source_ip"] or "", r["endpoint"],
              r["rule_id"], r["valid_from"], r["valid_to"])
        for r in db.rows(
            f"SELECT {_MATCH_COLS} FROM allowlist"
            " WHERE status = ?"
            " AND ((source_ip IS NOT NULL AND source_ip != '')"
            "      OR (endpoint IS NOT NULL AND endpoint != ''))"
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to >= ?)",
            (STATUS_ACTIVE, at, at)))


def build_index(entries: Iterable[Entry]) -> Index:
    """建立比對索引。

    刻意從 `index_by_ip()` 改名：回傳型別由 dict 變成 Index，而且多了一組
    「沒有 IP、只靠 endpoint」的條目。舊呼叫端必須 TypeError 而不是靜靜地
    只比對到一半（漏掉的那一半正是 R04 這種沒有來源 IP 的規則）。
    """
    by_ip: dict[str, list[Entry]] = {}
    by_rule: dict[str, list[Entry]] = {}
    for e in entries:
        if e.source_ip:
            by_ip.setdefault(e.source_ip, []).append(e)
        elif e.rule_id and e.endpoint:
            by_rule.setdefault(e.rule_id, []).append(e)
    return Index(by_ip={k: tuple(v) for k, v in by_ip.items()},
                 by_rule={k: tuple(v) for k, v in by_rule.items()})


def match(source_ips: Iterable[object], *, rule_id: str, endpoint: str,
          index: Index) -> Entry | None:
    """命中的條目，或 None。

    回傳 Entry 而不是 bool：抑制必須說得出「是**哪一條**例外遮掉了它」，
    否則掃描頁與規則頁只能顯示一個數字，而數字無法用來判斷例外該不該續期。

    `source_ips` 只該包含規則 entity 裡 `fp: src` 的欄位值。**不要傳
    entity_key 拆出來的所有片段** —— 那裡面第一段是 rule id，後面還有帳號與
    route，任何一段字面相符就抑制的話，一筆 source_ip='R01' 的條目會讓整條
    R01 失效（見 rules/engine.py 的 _allowlist_hit）。

    endpoint 是**完全相等**比對，不是前綴。比的是規則自己聚合出來的
    endpoint 值（例如 `Api2/GetProfile`），前綴比對會連
    `Api2/GetProfileExtra` 一起放行。
    """
    # 先看「只對某規則 + 某端點」的條目。這類沒有 IP，所以以 rule_id 為索引。
    if endpoint:
        for e in index.by_rule.get(rule_id, ()):
            if e.endpoint == endpoint:
                return e
    for raw in source_ips:
        ip = str(raw).strip() if raw is not None else ""
        if not ip:
            continue
        for e in index.by_ip.get(ip, ()):
            if e.rule_id is not None and e.rule_id != rule_id:
                continue
            if e.endpoint and e.endpoint != endpoint:
                continue
            return e
    return None


def global_source_ips(*, now: str | None = None) -> frozenset[str]:
    """**只有全域**條目的來源 IP，給期間掃描用。

    掃描不跑規則，所以規則範圍的例外對它沒有意義；漏掉 `rule_id IS NULL`
    的話，一筆「只對 R07B」的條目會讓那個來源從整份掃描報告消失。

    掃描層刻意不看 endpoint —— 掃描的對象就是來源本身。
    """
    at = now or timewin.fmt(timewin.taipei_now())
    return frozenset(
        r["source_ip"] for r in db.rows(
            "SELECT source_ip FROM allowlist"
            " WHERE status = ? AND rule_id IS NULL"
            " AND source_ip IS NOT NULL AND source_ip != ''"
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to >= ?)",
            (STATUS_ACTIVE, at, at)))


# ─────────────────────────── API 用的查詢 ───────────────────────────

def get(entry_id: int) -> dict | None:
    return db.one("SELECT * FROM allowlist WHERE id = ?", (entry_id,))


def rows(*, status: str | None = None, rule_id: str | None = None,
         scope: str | None = None, q: str | None = None,
         limit: int = 200) -> list[dict]:
    """條件查詢。scope: 'global'（rule_id IS NULL）/ 'rule' / None。"""
    sql = ["SELECT * FROM allowlist WHERE 1=1"]
    params: list[object] = []
    if status:
        sql.append("AND status = ?")
        params.append(status)
    if rule_id:
        sql.append("AND rule_id = ?")
        params.append(rule_id)
    if scope == "global":
        sql.append("AND rule_id IS NULL")
    elif scope == "rule":
        sql.append("AND rule_id IS NOT NULL")
    if q:
        sql.append("AND (name LIKE ? OR source_ip LIKE ? OR owner LIKE ?)")
        params += [f"%{q}%"] * 3
    sql.append("ORDER BY id DESC LIMIT ?")
    params.append(limit)
    return db.rows(" ".join(sql), tuple(params))


def conflict(source_ip: str, rule_id: str | None, endpoint: str = "",
             *, exclude_id: int | None = None) -> dict | None:
    """同一個 (IP, 範圍, 端點) 已有生效中的條目就回傳它。

    刻意用應用層檢查而不是唯一索引：正確的唯一性語意是
    `(source_ip, COALESCE(rule_id,'*'), COALESCE(endpoint,'')) WHERE status <> '已停用'`，
    而 CREATE UNIQUE INDEX 在既有 DB 有重複資料時**會建立失敗**，
    那個例外發生在 db.get_conn() 裡 —— 整站 500 而 /healthz 照樣 200。
    一個機制比「有時有索引有時沒有」清楚。
    """
    sql = ["SELECT * FROM allowlist WHERE status = ?"
           " AND COALESCE(source_ip, '') = ? AND COALESCE(endpoint, '') = ?"]
    params: list[object] = [STATUS_ACTIVE, source_ip or "", endpoint or ""]
    sql.append("AND rule_id IS NULL" if rule_id is None else "AND rule_id = ?")
    if rule_id is not None:
        params.append(rule_id)
    if exclude_id is not None:
        sql.append("AND id != ?")
        params.append(exclude_id)
    return db.one(" ".join(sql) + " LIMIT 1", tuple(params))


# ─────────────────────────── 寫入 ───────────────────────────

_WRITABLE = ("name", "purpose", "reason", "rule_id", "endpoint",
             "valid_from", "valid_to")

# `owner`（畫面上的「創立人」）**刻意不在 _WRITABLE 裡**：它由 `create()` 直接
# 從 `who` 寫入，之後不可修改。原本它是使用者可填的「負責人」，留空才帶登入帳號 ——
# 於是同一個欄位可能是任何字串（實測播種列是「Ocard 內部」，不是一個帳號）。
# 一個可以填任何字的欄位當不了「這筆核准是誰建的」的答案，而那正是唯一有稽核
# 意義的問題。放在這裡而不是只在 route 擋，是為了讓「不可修改」是結構性的：
# `update()` 收到 owner 會直接 KeyError 而不是靜靜改掉別人的核准紀錄。

# source_ip 刻意不可修改：一筆條目是「對某個特定來源的核准紀錄」，就地改 IP
# 會讓 audit_log 裡「#12 核准了 1.2.3.4」在事後解讀成別的 IP —— 稽核痕跡被
# 靜靜改寫。要換 IP 就停用 + 新增。


def create(fields: dict, *, who: str) -> int:
    """新增一筆生效中的條目，回傳 id。呼叫端負責驗證與寫稽核。

    `owner`（創立人）與 `approved_by` 都由 `who` 決定，呼叫端給不了 ——
    兩者在新資料上必然相同，這是刻意的：`approved_by` 已經被
    `allowlist_view` 用來判斷「是不是排程播種的」（`seeded`），
    語意是「這筆是誰放進來的」；`owner` 是畫面上顯示的那一個。
    分開留著是為了不動既有欄位，**2026-08 之前建立的列兩者可能不同**
    （舊版的 owner 是使用者自填的「負責人」）。
    """
    now = timewin.fmt(timewin.taipei_now())
    cols = list(_WRITABLE) + ["owner", "source_ip", "status", "approved_by",
                              "created_at", "updated_at", "updated_by"]
    values = [fields.get(c) for c in _WRITABLE] + [
        who, fields.get("source_ip") or None, STATUS_ACTIVE, who, now, now, who]
    placeholders = ",".join("?" * len(cols))
    with db.tx() as conn:
        cur = conn.execute(
            f"INSERT INTO allowlist ({','.join(cols)}) VALUES ({placeholders})",
            tuple(values))
        return int(cur.lastrowid)


def update(entry_id: int, fields: dict, *, who: str) -> None:
    """就地修改。fields 只能含 _WRITABLE 的鍵（呼叫端已驗證）。"""
    sets = [f"{c} = ?" for c in fields] + ["updated_at = ?", "updated_by = ?"]
    params = list(fields.values()) + [timewin.fmt(timewin.taipei_now()), who, entry_id]
    with db.tx() as conn:
        conn.execute(f"UPDATE allowlist SET {','.join(sets)} WHERE id = ?", tuple(params))


def set_status(entry_id: int, status: str, *, who: str, reason: str) -> None:
    """停用／恢復。**沒有 DELETE** —— audit_log.target 裡的 #id 必須永遠解得回一筆。"""
    with db.tx() as conn:
        conn.execute(
            "UPDATE allowlist SET status = ?, reason = ?, updated_at = ?, updated_by = ?"
            " WHERE id = ?",
            (status, reason, timewin.fmt(timewin.taipei_now()), who, entry_id))
