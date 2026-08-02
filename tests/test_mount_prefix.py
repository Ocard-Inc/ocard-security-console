"""SPA 掛載前綴：index.html 的靜態資源與 API 路徑都必須帶上掛載點。

這個檔案守的是一個**不會報錯**的部署失敗：主控台掛在 ROS 的 `/security`
底下時，瀏覽器最終停在沒有尾斜線的 `/security`（Next.js 預設的
trailingSlash: false 會把 `/security/` 導回去）。如果 index.html 用相對路徑
`./static/app.css`，它會解析成 `/static/app.css` —— 那是 ROS 的路徑，
不是主控台的。結果是整頁沒有樣式也沒有 JS，HTTP 狀態全部 200，
log 裡什麼都沒有。

所以改由後端以 `console_mount_path()`（推導自 `CONSOLE_BASE_URL`）注入。
下面同時測兩個方向：沒設掛載點時退回根路徑，設了就每個路徑都帶前綴。
"""
from __future__ import annotations

import re

import pytest

from console.api import app as app_mod

# index.html 裡所有 href/src 的值
_URLS = re.compile(r'(?:href|src)="([^"]+)"')


def _urls(html: str) -> list[str]:
    """只看自家資源；Google Fonts 是絕對網址，與掛載點無關。"""
    return [u for u in _URLS.findall(html) if not u.startswith("http")]


@pytest.fixture
def mount(monkeypatch):
    """改 CONSOLE_BASE_URL 並清掉 index.html 的快取。

    刻意設環境變數而不是 monkeypatch `console_mount_path` —— 這樣測到的是
    「CONSOLE_BASE_URL → 掛載路徑 → HTML」的完整推導鏈，而不是我對 app.py
    匯入寫法的假設（實際踩過：patch `config.console_mount_path` 對
    `from … import console_mount_path` 完全無效，測試看起來過了卻沒驗到）。
    `console_base_url()` 刻意沒有 lru_cache，所以設了就生效。
    """
    def _set(base_url: str) -> None:
        monkeypatch.setenv("CONSOLE_BASE_URL", base_url)
        app_mod._index_html.cache_clear()
    yield _set
    app_mod._index_html.cache_clear()


def test_index_leaves_no_placeholder(client):
    """{{MOUNT}} 沒被取代的話，瀏覽器會去要一個字面上叫 {{MOUNT}} 的路徑。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "{{MOUNT}}" not in r.text, "index.html 的佔位符未被取代"
    assert 'window.__MOUNT__ = "' in r.text


def test_no_relative_asset_paths(client):
    """`./static/…` 是這個 bug 的原始寫法，不可以回來。

    相對路徑在有尾斜線時看起來完全正常，所以只靠人工測試抓不到。
    """
    html = client.get("/").text
    relative = [u for u in _urls(html) if u.startswith(".")]
    assert not relative, f"這些資源用了相對路徑，換掛載點就會壞：{relative}"


def test_paths_are_root_relative_without_mount(client, mount):
    """網址沒有子路徑（本機開發）時，資源路徑就是 /static/…。"""
    mount("http://127.0.0.1:8600")
    html = client.get("/").text
    for u in _urls(html):
        assert u.startswith("/static/"), f"未預期的資源路徑：{u}"
    assert 'window.__MOUNT__ = ""' in html


def test_every_asset_carries_the_mount_prefix(client, mount):
    """掛在 /security 底下時，**每一個**自家資源都要帶前綴 —— 漏一個就是
    一個靜靜 404 的檔案（少了 charts.css 只會讓圖表變形，不會有錯誤）。"""
    mount("https://ros.ocard.co/security")
    html = client.get("/").text
    assets = _urls(html)
    assert assets, "index.html 沒有任何自家資源，測試等於沒驗到東西"
    for u in assets:
        assert u.startswith("/security/static/"), f"缺少掛載前綴：{u}"
    # 前端組 API 網址用的也是同一個值
    assert 'window.__MOUNT__ = "/security"' in html


def test_trailing_slash_in_config_does_not_double_up(client, mount):
    """CONSOLE_BASE_URL 寫成 `…/security/` 時不可以生出 `/security//static/…`。
    多一條斜線多數伺服器會容忍，但 StaticFiles 會回 404。"""
    mount("https://ros.ocard.co/security/")
    for u in _urls(client.get("/").text):
        assert u.startswith("/security/static/"), f"路徑組壞了：{u}"
        assert "//" not in u, f"出現重複斜線：{u}"


def test_all_head_assets_present(client):
    """注入不能順手漏掉檔案。這四個 CSS 與兩個 JS 是畫面的最小集合。"""
    html = client.get("/").text
    for name in ("ds/colors_and_type.css", "vendor/apexcharts-6.7.0.css",
                 "app.css", "charts/charts.css",
                 "vendor/apexcharts-6.7.0.min.js", "app.js"):
        assert f"/static/{name}" in html, f"index.html 少了 {name}"


def test_apexcharts_umd_is_not_async(client):
    """ApexCharts 的 UMD 必須在 module script 之前執行（見 index.html 註解）。
    加了 async 只會在慢速網路下壞掉，本機永遠測不出來。"""
    html = client.get("/").text
    umd = next(line for line in html.splitlines() if "apexcharts-6.7.0.min.js" in line)
    assert "async" not in umd and "defer" not in umd, umd.strip()
