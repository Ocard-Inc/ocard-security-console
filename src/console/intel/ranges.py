"""離線 CIDR 比對：IP → (來源型態, 組織, 區域)。

## 資料從哪來

`data/cloud_ranges/` 放**上游原封不動的檔案**，版本寫在檔名裡（升級 = 下載新日期的檔案、
改 `FILES` 的日期常數），比照 `web/vendor/` 的慣例。兩類：

- `aws` / `gcp` / `oracle` / `cloudflare` / `digitalocean` / `linode`
  —— 業者自己公開的 IP 範圍檔，權威且會持續更新。
- `asn-*.json` —— 由 RIPEstat 的 announced-prefixes 取得的 ASN 公告前綴快照。
  給沒有公開範圍檔的業者用（騰訊雲、小型 VPS 商、商業 VPN）。
  **查詢時只送 AS 號，不含任何我方資料**；落地後純離線比對。

全部離線。這個模組不發任何網路請求 —— 原始 IP 不得離開 process，
連「送去查歸屬」也不行（見 CLAUDE.md 的遮罩約束）。

## 為什麼需要索引

約 23,000 個 CIDR × 約 40,000 個相異 IP = 9 億次比對，逐條線性掃要跑幾十分鐘。
以 IP 的前 16 bits 分桶：每個 CIDR 註冊到它涵蓋的所有 /16 桶，查詢時只比對
同桶內的少數幾條。比 /16 更大的網段（如 104.16.0.0/13 涵蓋 8 個 /16）會註冊多次，
總量仍在數萬級。實測 40,000 個 IP 分類在 1 秒內完成。
"""
from __future__ import annotations

import csv
import ipaddress
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from console.core.config import CONFIG_DIR

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "cloud_ranges"

# 目前使用的檔案版本。升級 = 下載新日期的檔案並改這裡（舊檔可留著備查）。
SNAPSHOT = "20260803"

HOSTING = "hosting"
VPN = "vpn"

# 業者自己公開的範圍檔。(檔名, 解析器, 組織名, 型態)
_PUBLISHED = (
    (f"aws-ip-ranges-{SNAPSHOT}.json", "aws", "Amazon Web Services", HOSTING),
    (f"gcp-cloud-{SNAPSHOT}.json", "gcp", "Google Cloud", HOSTING),
    (f"oracle-public-ranges-{SNAPSHOT}.json", "oracle", "Oracle Cloud", HOSTING),
    (f"digitalocean-geo-{SNAPSHOT}.csv", "csv_cidr_first", "DigitalOcean", HOSTING),
    (f"linode-geoip-{SNAPSHOT}.csv", "csv_cidr_first", "Linode", HOSTING),
)

# Cloudflare 的 CDN／反向代理位址。**刻意單獨處理、且不分類為 vpn** ——
# 這些是 Cloudflare 面對外界的前端位址，不是使用者的出口位址。若我方某個路徑
# 記到了 CDN 位址，那是「取 IP 的邏輯取到了代理跳點」的資料品質問題，
# 與「使用者用消費級 VPN 隱藏來源」是完全不同的結論，不可混為一談。
#
# WARP（消費級 VPN，報告事件 9 的 104.28.128.x）屬 AS13335 但**不在**這份 CDN 清單裡，
# 所以「AS13335 且不在 CDN 清單」正好就是 WARP 這類客戶端出口。
_CLOUDFLARE_CDN = f"cloudflare-ips-v4-{SNAPSHOT}.txt"

_ASN_FILES = (
    f"asn-as18526-{SNAPSHOT}.json",
    f"asn-as209854-{SNAPSHOT}.json",
    f"asn-as132203-{SNAPSHOT}.json",
    f"asn-as45090-{SNAPSHOT}.json",
    f"asn-as13335-{SNAPSHOT}.json",
)

MANUAL_FILE = CONFIG_DIR / "ip_intel.yaml"


@dataclass(frozen=True)
class Attribution:
    source_type: str
    org: str
    region: str = ""
    note: str = ""


# 比對優先序（大者勝，同層再比前綴長度）。
#
# 為什麼不能只用「最長前綴」：ASN 快照是整個 AS 的公告前綴，裡面的條目往往比
# 業者自己公布的特殊用途網段**更長**。實測 Cloudflare CDN 的 104.16.0.0/13
# 會輸給 AS13335 快照裡的 104.16.0.0/20，於是 CDN 位址被判成 vpn ——
# 那是完全不同的結論（「取 IP 取到代理跳點」vs「使用者用 VPN 隱匿來源」）。
#
# 所以精確度要先看**資料的性質**再看前綴長度：人工覆寫 > 已知代理清單 >
# 業者公開範圍 > ASN 全集。
TIER_ASN = 0        # 整個 AS 的公告前綴，範圍最廣、語意最粗
TIER_PUBLISHED = 1  # 業者自己公布的範圍檔
TIER_CDN = 2        # 已知的 CDN／反向代理位址
TIER_MANUAL = 3     # config/ip_intel.yaml 的人工覆寫


@dataclass(frozen=True)
class _Entry:
    network: ipaddress.IPv4Network
    attribution: Attribution
    tier: int = TIER_ASN

    @property
    def precedence(self) -> tuple[int, int]:
        return (self.tier, self.network.prefixlen)


def _aws(path: Path, org: str, kind: str):
    d = json.loads(path.read_text(encoding="utf-8"))
    for p in d.get("prefixes", ()):
        if p.get("ip_prefix"):
            yield p["ip_prefix"], Attribution(kind, org, p.get("region", ""))


def _gcp(path: Path, org: str, kind: str):
    d = json.loads(path.read_text(encoding="utf-8"))
    for p in d.get("prefixes", ()):
        if p.get("ipv4Prefix"):
            yield p["ipv4Prefix"], Attribution(kind, org, p.get("scope", ""))


def _oracle(path: Path, org: str, kind: str):
    d = json.loads(path.read_text(encoding="utf-8"))
    for r in d.get("regions", ()):
        for c in r.get("cidrs", ()):
            if c.get("cidr"):
                yield c["cidr"], Attribution(kind, org, r.get("region", ""))


def _csv_cidr_first(path: Path, org: str, kind: str):
    """第一欄是 CIDR 的 CSV（DigitalOcean / Linode 的地理檔）。"""
    for row in csv.reader(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if not row or not row[0] or row[0].lstrip().startswith("#"):
            continue
        if "/" not in row[0]:
            continue
        region = "-".join(x for x in row[1:3] if x) if len(row) > 1 else ""
        yield row[0].strip(), Attribution(kind, org, region)


_PARSERS = {"aws": _aws, "gcp": _gcp, "oracle": _oracle,
            "csv_cidr_first": _csv_cidr_first}


def _manual_entries():
    """config/ip_intel.yaml 的人工清單。缺檔不是錯誤（只是沒有人工覆寫）。"""
    if not MANUAL_FILE.exists():
        return
    import yaml
    data = yaml.safe_load(MANUAL_FILE.read_text(encoding="utf-8")) or {}
    for item in data.get("networks", ()):
        cidr = item.get("cidr")
        if not cidr:
            continue
        yield cidr, Attribution(
            source_type=str(item.get("source_type", "unknown")),
            org=str(item.get("org", "")),
            region=str(item.get("region", "")),
            note=str(item.get("note", "")),
        )


def _load_entries() -> list[_Entry]:
    entries: list[_Entry] = []

    def add(cidr: str, attribution: Attribution, tier: int) -> None:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            logger.warning("忽略無法解析的 CIDR %r", cidr)
            return
        if isinstance(net, ipaddress.IPv4Network):
            entries.append(_Entry(net, attribution, tier))

    for name, parser, org, kind in _PUBLISHED:
        path = DATA_DIR / name
        if not path.exists():
            logger.warning("缺少範圍檔 %s，該業者不會被分類", name)
            continue
        for cidr, attribution in _PARSERS[parser](path, org, kind):
            add(cidr, attribution, TIER_PUBLISHED)

    for name in _ASN_FILES:
        path = DATA_DIR / name
        if not path.exists():
            logger.warning("缺少 ASN 快照 %s", name)
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        attribution = Attribution(doc["source_type"], doc["org"], doc["asn"])
        for cidr in doc.get("prefixes", ()):
            add(cidr, attribution, TIER_ASN)

    # Cloudflare 的 CDN 清單放在 TIER_CDN，才會贏過 AS13335 快照裡更長的前綴。
    # 剩下「屬 AS13335 但不在 CDN 清單」的部分維持 vpn —— 那正好是 WARP
    # 這類消費級 VPN 的客戶端出口（報告事件 9 的 104.28.128.x）。
    cdn = DATA_DIR / _CLOUDFLARE_CDN
    if cdn.exists():
        for line in cdn.read_text(encoding="utf-8").split():
            if line.strip():
                add(line.strip(), Attribution(
                    "cdn_proxy", "Cloudflare CDN", "",
                    "Cloudflare 面向外界的反向代理位址，不是使用者出口。"
                    "後台日誌若記到這裡，代表取 IP 的邏輯取到了代理跳點。"),
                    TIER_CDN)

    for cidr, attribution in _manual_entries():
        add(cidr, attribution, TIER_MANUAL)

    return entries


@lru_cache(maxsize=1)
def _index() -> dict[int, tuple[_Entry, ...]]:
    """/16 分桶索引。見模組說明為何需要它。"""
    buckets: dict[int, list[_Entry]] = {}
    for e in _load_entries():
        first = int(e.network.network_address) >> 16
        last = int(e.network.broadcast_address) >> 16
        # 比 /16 大的網段會涵蓋多個桶；/8 最多 256 個，總量仍在數萬級。
        for key in range(first, last + 1):
            buckets.setdefault(key, []).append(e)
    return {k: tuple(v) for k, v in buckets.items()}


def stats() -> dict:
    """索引概況（給 CLI 與限制段落用）。"""
    entries = _load_entries()
    by_org: dict[str, int] = {}
    for e in entries:
        by_org[e.attribution.org] = by_org.get(e.attribution.org, 0) + 1
    return {"snapshot": SNAPSHOT, "networks": len(entries),
            "buckets": len(_index()), "by_org": by_org}


def lookup(ip: str) -> Attribution | None:
    """依 (tier, 前綴長度) 取最精確的歸屬。找不到回 None。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    candidates = _index().get(int(addr) >> 16)
    if not candidates:
        return None
    best: _Entry | None = None
    for e in candidates:
        if addr in e.network and (best is None or e.precedence > best.precedence):
            best = e
    return best.attribution if best else None


def reload_cache() -> None:
    """測試與 CLI 用：清掉索引快取。"""
    _index.cache_clear()
