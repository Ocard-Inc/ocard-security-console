"""來源情報（ip_intel）的讀取端。

寫入端是 `console.intel.refresh`（掃 ClickHouse 的相異來源 → 以
`data/cloud_ranges/` 的離線範圍檔與 `config/ip_intel.yaml` 分類 → 寫入）。
表是空的時候：

- `available()` 回 False → `run.run_probes()` 自動跳過 `needs_intel` 的探針
- `limits.collect()` 明確註明「來源型態未涵蓋」

這是刻意的降級路徑：沒有情報就明說沒查，而不是回報「沒有異常」。

**表裡只有 fingerprint 與分類，沒有原始 IP。** 分類（機房／VPN／住宅）與歸屬
（ASN 組織、國別）不是個資，把它們與 fingerprint 一起存不會讓系統獲得還原能力。
"""
from __future__ import annotations

from console.store import db

# 分類值與標籤的唯一真相在 console.intel.classify —— 這裡不要再定義一份，
# 兩份常數遲早會不一致。


def available() -> bool:
    """ip_intel 是否有資料。空表時所有 needs_intel 的探針都會被跳過。"""
    row = db.one("SELECT count(*) AS n FROM ip_intel")
    return bool(row and row["n"])


def lookup(src_fps: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """批次查分類。回傳 src → {source_type, org, country, note}。"""
    if not src_fps:
        return {}
    placeholders = ",".join("?" * len(src_fps))
    rows = db.rows(
        f"SELECT src, source_type, org, country, note FROM ip_intel"
        f" WHERE src IN ({placeholders})", tuple(src_fps))
    return {r.pop("src"): r for r in rows}


def coverage() -> dict:
    """情報涵蓋概況，供限制段落呈現。

    注意這是 SQLite 不是 ClickHouse —— 沒有 countIf()，用
    `sum(條件)` 取代（SQLite 的布林是 0/1）。
    """
    row = db.one(
        "SELECT count(*) AS total,"
        " coalesce(sum(source_type = 'hosting'), 0) AS hosting,"
        " coalesce(sum(source_type = 'vpn'), 0) AS vpn"
        " FROM ip_intel")
    if not row:
        return {"total": 0, "hosting": 0, "vpn": 0}
    return {"total": int(row["total"] or 0), "hosting": int(row["hosting"] or 0),
            "vpn": int(row["vpn"] or 0)}
