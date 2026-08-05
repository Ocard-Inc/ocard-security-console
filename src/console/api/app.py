"""FastAPI 應用：Security Log Console 後端 + SPA 靜態檔。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from console.alerting import notify
from console.api import allowlist_routes, audit_routes, routes, rules_routes
from console.auth import ros
from console.checker.scheduler import scheduler_loop
from console.core.config import WEB_DIR, console_mount_path, settings
from console.core.logging_setup import setup_logging

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _index_html() -> str:
    """index.html 的 {{MOUNT}} 換成實際掛載路徑。

    為什麼由後端注入，而不是讓前端從 `location.pathname` 推導：掛在 ROS 的
    `/security` 底下時，瀏覽器最終停在**沒有尾斜線**的 `/security`
    （Next.js 預設的 trailingSlash: false 會把 `/security/` 導回去），
    於是 `./static/app.css` 解析成 `/static/app.css` —— 打到 ROS，
    整頁沒有樣式也沒有 JS，而且沒有任何錯誤訊息指向原因。

    lru_cache：掛載路徑在 process 生命週期內固定（來自 CONSOLE_BASE_URL，
    而 config 的 getter 全部 lru_cache，改 .env 一律要重啟）。
    """
    return (WEB_DIR / "index.html").read_text(encoding="utf-8") \
        .replace("{{MOUNT}}", console_mount_path())


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("console.log")
    # 必須排在 setup_logging 之後 —— 在那之前 WARNING 還沒有 handler 可以落到
    # state/logs/console.log，而這一行的整個用途就是留下痕跡。
    notify.log_startup_status()
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    logger.info("Security Log Console 啟動，五分鐘檢查已排程")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("已停止")


app = FastAPI(title="Ocard Security Log Console", lifespan=lifespan)
app.include_router(routes.router, prefix="/api")
# 規則／Allowlist／稽核拆成獨立模組（routes.py 已經 800 行），但 URL 前綴一律
# /api —— 前端的 lib.js api() 只加 /api，不認別的前綴。
app.include_router(rules_routes.router, prefix="/api")
app.include_router(allowlist_routes.router, prefix="/api")
app.include_router(audit_routes.router, prefix="/api")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    """前端是無建置流程的 ES module，瀏覽器快取會讓改動不生效，所以自家檔案一律 no-store。

    例外是 /static/vendor/：那裡只放未修改的上游檔案，版本寫在檔名裡（升級 = 改名），
    內容永不就地變更，所以可以永久快取。ApexCharts 有 886 KB，沒有這個例外的話
    每次重新整理都要重抓。分支順序不可對調 —— /static/vendor/ 是 /static/ 的前綴。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
async def index(request: Request):
    """SPA 入口。未登入時直接 302 到 ROS 登入頁 —— 讓使用者看到的是登入畫面，
    而不是先閃一下空殼主控台再被前端踢走。"""
    if ros.enabled():
        try:
            if ros.resolve_user(dict(request.cookies)) is None:
                return RedirectResponse(ros.login_url("/"), status_code=302)
        except ros.RosUnavailable:
            # ROS 不可用時仍載入 SPA，由前端顯示明確的錯誤（而非把人丟到登入頁繞圈）
            logger.warning("ROS 驗證不可用，改由前端呈現錯誤")
    return HTMLResponse(_index_html())


@app.get("/healthz")
async def healthz() -> dict:
    """給 reverse proxy / 排程 watchdog 用的存活檢查，不需登入。"""
    return {"ok": True}


def main() -> None:
    import uvicorn
    cfg = settings()["app"]
    uvicorn.run("console.api.app:app", host=cfg["host"], port=cfg["port"], reload=False)


if __name__ == "__main__":
    main()
