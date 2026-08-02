"""IP → 來源型態。純函式，不查 DB、不發網路請求。

分類順序（前面的先成立就不再往下）：

1. **字串層**：多段 XFF、私有位址、loopback —— 這些不需要任何外部資料就能判定，
   而且比歸屬更重要：一個記到內網位址的欄位，談「它屬於哪家業者」毫無意義。
2. **CIDR 比對**：`ranges.lookup()`，見那個模組的優先序說明。
3. **unknown**：查不到就是查不到。**不要**預設成 residential —— 那會把
   「沒有資料」偷換成「不是機房」，正好是報告一再警告的那種錯誤。
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from console.intel import ranges

# 來源型態。與 config/ip_intel.yaml 的說明、sweep 的探針判定共用同一組值。
HOSTING = "hosting"
VPN = "vpn"
RESIDENTIAL = "residential"
OFFICE = "office"
CDN_PROXY = "cdn_proxy"
PRIVATE = "private"          # 內網位址（取 IP 的邏輯取到了內部 hop）
FORGED = "forged"            # 多段字串且首段為私有位址 —— 刻意偽造
MULTI_HOP = "multi_hop"      # 多段字串，首段是公網位址（代理鏈未正規化）
UNKNOWN = "unknown"

# 「真人不會從這裡登入後台」—— 報告最強的單一訊號。
SUSPICIOUS_TYPES = frozenset({HOSTING, VPN})

# 這些型態代表「日誌記到的不是真實客戶端」，屬資料品質／規避行為，
# 由 sweep 的 P09 以字串規則獨立處理，這裡只負責標記。
UNTRUSTED_TYPES = frozenset({PRIVATE, FORGED, MULTI_HOP})

LABELS = {
    HOSTING: "機房／雲端主機", VPN: "VPN／匿名代理", RESIDENTIAL: "住宅或企業寬頻",
    OFFICE: "我方辦公室出口", CDN_PROXY: "CDN／企業代理", PRIVATE: "內網位址",
    FORGED: "偽造來源標頭", MULTI_HOP: "未正規化的代理鏈", UNKNOWN: "歸屬未知",
}


@dataclass(frozen=True)
class Classification:
    source_type: str
    org: str = ""
    country: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.source_type, self.source_type)

    @property
    def suspicious(self) -> bool:
        return self.source_type in SUSPICIOUS_TYPES


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def classify(raw_ip: str | None) -> Classification:
    """分類單一 IP 欄位值。`raw_ip` 可能是多段 XFF 字串。"""
    value = (raw_ip or "").strip()
    if not value:
        return Classification(UNKNOWN, note="日誌未記錄來源")

    # 1. 字串層：多段與私有位址
    if "," in value:
        first = value.split(",")[0].strip()
        if _is_private(first):
            return Classification(
                FORGED, note="多段 X-Forwarded-For 的首段是私有位址 —— "
                             "客戶端送出的偽造值，沒有正當用途")
        # 首段是公網：代理鏈未正規化。歸屬以**首段**為準（那才是宣稱的客戶端）
        inner = ranges.lookup(first)
        return Classification(
            MULTI_HOP,
            org=inner.org if inner else "",
            country=inner.region if inner else "",
            note="IP 欄位是未正規化的代理鏈；歸屬取自首段（客戶端可控，屬未驗證值）")
    if _is_private(value):
        return Classification(
            PRIVATE, note="內網／loopback 位址 —— 取 IP 的邏輯取到了內部 hop，"
                          "或開發測試流量混入正式日誌")

    # 2. CIDR 比對
    found = ranges.lookup(value)
    if found is not None:
        return Classification(found.source_type, org=found.org,
                              country=found.region, note=found.note)

    # 3. 查不到就是查不到 —— 不要預設成 residential
    return Classification(UNKNOWN, note="離線範圍檔與人工清單皆未涵蓋此位址")
