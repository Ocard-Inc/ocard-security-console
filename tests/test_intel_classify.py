"""來源型態分類。純函式，不連 ClickHouse（只讀 data/cloud_ranges/ 與 config/ip_intel.yaml）。

守三件事：

1. **優先序**：ASN 快照的前綴往往比業者公布的特殊用途網段更長，光靠「最長前綴」
   會把 Cloudflare 的 CDN 位址判成 vpn —— 那是完全不同的結論。
2. **查不到 ≠ 住宅寬頻**：未涵蓋的位址必須是 unknown。把它預設成 residential
   等於把「沒有資料」偷換成「不是機房」，正是報告一再警告的錯誤。
3. **字串層優先於歸屬**：一個記到內網位址的欄位，談「它屬於哪家業者」毫無意義。
"""
from __future__ import annotations

import pytest

from console.intel import classify, ranges


@pytest.fixture(scope="module", autouse=True)
def _fresh_index():
    ranges.reload_cache()
    yield
    ranges.reload_cache()


# ── 優先序 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip, expected, why", [
    # 業者自己公布的範圍檔（報告事件 1/4/5/6/10 的來源）
    ("13.112.100.38", classify.HOSTING, "AWS 東京 — 報告事件 5（113 個帳號共用）"),
    ("13.125.88.63", classify.HOSTING, "AWS 首爾 — 事件 1 的探測日來源"),
    ("158.179.177.178", classify.HOSTING, "Oracle 東京 — 事件 6"),
    ("140.245.49.162", classify.HOSTING, "Oracle — 事件 10 的 ocardsystemadmin"),
    # ASN 快照補上沒有公開範圍檔的業者
    ("131.143.215.229", classify.HOSTING, "DDPS Networks AS18526 — 事件 1 的攻擊 IP"),
    ("43.167.242.81", classify.HOSTING, "騰訊雲 AS132203 — 事件 4"),
    ("89.117.42.78", classify.VPN, "Cyberzone AS209854 — 事件 3 的匿名出口"),
    ("104.28.128.19", classify.VPN, "Cloudflare WARP — 事件 9（消費級 VPN）"),
    # 人工清單
    ("1.34.41.218", classify.OFFICE, "我方辦公室出口"),
    ("125.227.243.115", classify.RESIDENTIAL, "HiNet — 事件 8"),
    ("136.226.240.197", classify.CDN_PROXY, "Zscaler 企業代理（報告判為正常）"),
])
def test_known_sources_classify_as_expected(ip, expected, why) -> None:
    assert classify.classify(ip).source_type == expected, why


def test_cdn_beats_asn_snapshot() -> None:
    """Cloudflare 的 CDN 位址必須是 cdn_proxy，不是 vpn。

    這是最容易錯的一格：CDN 清單的 104.16.0.0/13 比 AS13335 快照裡的
    104.16.0.0/20 **短**，只看前綴長度會讓 ASN 勝出。兩者的結論完全不同 ——
    「取 IP 的邏輯取到了反向代理」vs「使用者用 VPN 隱匿來源」。
    """
    assert classify.classify("104.16.0.1").source_type == classify.CDN_PROXY
    assert classify.classify("172.64.0.1").source_type == classify.CDN_PROXY
    # 屬 AS13335 但不在 CDN 清單裡的才是 WARP 這類客戶端出口
    assert classify.classify("104.28.128.19").source_type == classify.VPN


def test_manual_override_beats_broader_automatic_entry() -> None:
    """人工清單的 /32 要贏過自己寫的 /16（同一份檔案內也靠前綴長度）。"""
    assert classify.classify("1.34.41.218").source_type == classify.OFFICE
    assert classify.classify("1.34.99.1").source_type == classify.RESIDENTIAL


# ── 查不到就是查不到 ───────────────────────────────────────────────

def test_uncovered_address_is_unknown_not_residential() -> None:
    """8.8.8.8 不在任何範圍檔裡。它必須是 unknown。

    若哪天有人把 unknown 的預設改成 residential，這條會失敗 —— 那個改動會讓
    「我們沒有這個位址的資料」在報告上長成「這不是機房」。
    """
    c = classify.classify("8.8.8.8")
    assert c.source_type == classify.UNKNOWN
    assert not c.suspicious
    assert "未涵蓋" in c.note


def test_empty_and_malformed_values_do_not_raise() -> None:
    for value in (None, "", "   ", "not-an-ip", "999.999.999.999", "::1"):
        assert classify.classify(value).source_type in (
            classify.UNKNOWN, classify.PRIVATE)


# ── 字串層優先 ─────────────────────────────────────────────────────

def test_forged_xff_detected_from_first_hop() -> None:
    """多段 XFF 且首段是私有位址 = 客戶端送出的偽造值。

    報告事件 1 的前置探測就是這一筆（`127.0.0.1, 13.125.88.63`）。第二段是
    AWS 首爾機房，但**結論必須是 forged 而不是 hosting** —— 重點是有人偽造來源，
    不是那台機器在哪。
    """
    c = classify.classify("127.0.0.1, 13.125.88.63")
    assert c.source_type == classify.FORGED
    assert "偽造" in c.note


def test_multi_hop_with_public_first_hop_is_not_forged() -> None:
    """首段是公網位址的代理鏈只是未正規化，不是偽造。

    報告附錄 A：多段 XFF 全期 28,701 筆，多數是 Zscaler 企業代理，屬正常。
    歸屬取自首段（宣稱的客戶端），並註明那是客戶端可控的未驗證值。
    """
    c = classify.classify("59.120.180.224, 136.226.240.197")
    assert c.source_type == classify.MULTI_HOP
    assert c.source_type not in (classify.FORGED,)
    assert "未驗證" in c.note


@pytest.mark.parametrize("ip", ["192.168.97.1", "172.18.0.1", "10.0.0.5",
                                "127.0.0.1", "169.254.1.1"])
def test_private_addresses_classified_before_attribution(ip) -> None:
    assert classify.classify(ip).source_type == classify.PRIVATE


def test_suspicious_flag_only_covers_hosting_and_vpn() -> None:
    """`suspicious` 是「真人不會從這裡登入後台」，不是「有問題」。

    內網位址與代理鏈是資料品質問題（由 P09 獨立處理），住宅寬頻與辦公室出口
    是正常來源 —— 都不該讓 P07/P08 命中。
    """
    assert classify.Classification(classify.HOSTING).suspicious
    assert classify.Classification(classify.VPN).suspicious
    for t in (classify.RESIDENTIAL, classify.OFFICE, classify.CDN_PROXY,
              classify.PRIVATE, classify.FORGED, classify.MULTI_HOP,
              classify.UNKNOWN):
        assert not classify.Classification(t).suspicious, t


# ── 索引 ──────────────────────────────────────────────────────────

def test_index_is_loaded_and_nontrivial() -> None:
    s = ranges.stats()
    assert s["networks"] > 10_000, "範圍檔沒載到（data/cloud_ranges/ 缺檔？）"
    assert s["buckets"] > 100
    # 每一家都要有東西，缺檔會讓對應的業者靜靜地不被分類
    for org in ("Amazon Web Services", "Oracle Cloud", "Google Cloud",
                "DDPS Networks", "Cyberzone S.A."):
        assert s["by_org"].get(org), f"{org} 的範圍資料缺失"


def test_every_label_has_a_chinese_name() -> None:
    """新增型態卻忘了給標籤時，UI 會直接顯示英文鍵名。"""
    for t in (classify.HOSTING, classify.VPN, classify.RESIDENTIAL, classify.OFFICE,
              classify.CDN_PROXY, classify.PRIVATE, classify.FORGED,
              classify.MULTI_HOP, classify.UNKNOWN):
        assert classify.LABELS.get(t), t
