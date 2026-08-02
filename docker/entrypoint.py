"""容器啟動：取設定 → 斷言狀態磁碟 → 起 uvicorn。

兩件事刻意在這裡做，而不是寫進映像、也不是寫進 VM 的 instance metadata：

**一、`.env` 從 Secret Manager 取得，寫進容器的可寫層。**
instance metadata（含 konlet 的 `--container-env`）是明文，任何有
`compute.instances.get` 權限的人都讀得到，而且每一版都留著 —— 同 project 的
`ocard-data-api` 就是這樣把 ClickHouse 帳密、AWS key 攤在 revision 上。
寫進 `/app/.env` 而不是 persistent disk：容器消滅檔案就消滅，憑證不落在
會被快照、會被掛到別台機器的磁碟上。`core/config.py` 的 `load_dotenv()`
本來就讀這個路徑，所以應用程式碼不需要任何改動。

**二、斷言 state 磁碟真的掛上了。**
COS 上 `konlet-startup.service`（起容器）與 `google-startup-scripts.service`
（格式化並掛載磁碟）**沒有保證的先後順序**。磁碟還沒掛好容器就起來的話，
SQLite 會建在開機磁碟的 `/mnt/disks/state` 上，之後掛載把它整個遮住 ——
資料靜靜寫到錯的地方，不會有任何錯誤訊息，而下次開機「資料庫是空的」會被
當成首次啟動：`known_sources` 空表讓 R08A/B/C 把每個來源都當首見而洪水式告警，
`audit_log` 的調閱留痕消失。

哨兵檔在**佈建時建立於磁碟上**（見 scripts/provision_gcp.sh），所以磁碟沒掛好
時一定找不到。找不到就以非零狀態退出，konlet 的 restart policy 會重啟容器，
直到掛載完成 —— 失敗是大聲的、會自己復原的，不是靜靜寫錯地方。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
SECRET_ACCESS_URL = "https://secretmanager.googleapis.com/v1/{name}:access"

ENV_PATH = "/app/.env"
DEFAULT_SENTINEL = "/app/state/.disk-ok"


def _log(msg: str) -> None:
    """直接寫 stderr。此時 logging_setup 還沒跑，而 stderr 進 Cloud Logging。"""
    print(f"[entrypoint] {msg}", file=sys.stderr, flush=True)


def _fail(msg: str) -> None:
    _log(f"啟動中止：{msg}")
    raise SystemExit(1)


# ── Secret Manager ────────────────────────────────────────────────────

def _metadata_token() -> str:
    req = urllib.request.Request(
        METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["access_token"]


def _fetch_secret(name: str) -> str:
    """取回 secret 版本的明文。`name` 是完整資源名稱（.../versions/latest）。

    刻意用 stdlib 的 urllib 而不是 google-cloud-secret-manager：這支程式在
    依賴安裝完成後、應用程式匯入之前執行，多一個 SDK 只是多一個會壞的地方。
    """
    token = _metadata_token()
    req = urllib.request.Request(
        SECRET_ACCESS_URL.format(name=name),
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)["payload"]["data"]
    return base64.b64decode(payload).decode("utf-8")


def _write_env() -> None:
    """把 Secret Manager 的內容寫成 .env。未設定 CONSOLE_ENV_SECRET 就跳過。

    跳過是為了讓同一個映像能在本機以 `docker run -v $PWD/.env:/app/.env` 跑起來。
    但**正式環境不設這個變數等於沒有任何連線資訊** —— `_require_env()` 會在
    第一次查詢時拋 ConfigError，訊息明確指向缺少的變數，不會靜靜連錯地方。
    """
    name = os.environ.get("CONSOLE_ENV_SECRET", "").strip()
    if not name:
        _log("未設定 CONSOLE_ENV_SECRET，沿用映像／掛載中既有的 .env")
        return

    try:
        content = _fetch_secret(name)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        _fail(f"讀取 {name} 失敗（HTTP {exc.code}）：{body}\n"
              f"        VM 的 service account 需要 roles/secretmanager.secretAccessor")
    except Exception as exc:  # noqa: BLE001 — 任何失敗都不該讓容器帶著空設定啟動
        _fail(f"讀取 {name} 失敗：{type(exc).__name__}: {exc}")

    # 0600：容器內只有這個 process 會讀它
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    keys = [ln.split("=", 1)[0].strip() for ln in content.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#") and "=" in ln]
    _log(f"已寫入 {ENV_PATH}（{len(keys)} 個變數：{', '.join(sorted(keys))}）")


# ── 狀態磁碟 ──────────────────────────────────────────────────────────

def _assert_state_disk() -> None:
    """哨兵檔不在 = persistent disk 還沒掛上 = 不可以啟動。見模組說明。"""
    sentinel = os.environ.get("CONSOLE_STATE_SENTINEL", DEFAULT_SENTINEL)
    if not sentinel:
        _log("CONSOLE_STATE_SENTINEL 為空，跳過狀態磁碟檢查（僅適用本機）")
        return
    if not os.path.exists(sentinel):
        _fail(f"找不到哨兵檔 {sentinel} —— state 磁碟尚未掛載。\n"
              f"        容器將退出並由 konlet 重啟；若持續失敗，"
              f"在 VM 上檢查 `mount | grep /mnt/disks/state`。")
    if not os.access(os.path.dirname(sentinel), os.W_OK):
        _fail(f"{os.path.dirname(sentinel)} 不可寫入 —— SQLite 無法建立 WAL。")
    _log(f"state 磁碟已掛載（{sentinel}）")


# ── 啟動 ──────────────────────────────────────────────────────────────

def _port() -> int:
    """埠號以 config/settings.yaml 為單一真相，避免與 Dockerfile／防火牆規則漂移。"""
    sys.path.insert(0, "/app/src")
    from console.core.config import settings
    return int(settings()["app"]["port"])


def main() -> None:
    _write_env()
    _assert_state_disk()
    port = _port()
    # 單一 worker 是硬性要求，不是預設值 —— 排程器跑在 lifespan 內，
    # 多 worker 會讓同一個 tick 被評估多次並發出重複通知。
    argv = ["uvicorn", "console.api.app:app",
            "--host", "0.0.0.0", "--port", str(port), "--workers", "1"]
    _log(f"啟動 {' '.join(argv)}")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
