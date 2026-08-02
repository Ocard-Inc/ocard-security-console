"""FastAPI 應用：Security Log Console 後端 + SPA 靜態檔。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from console.api import routes
from console.auth import ros
from console.checker.scheduler import scheduler_loop
from console.core.config import WEB_DIR, settings
from console.core.logging_setup import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("console.log")
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
async def index(request: Request) -> HTMLResponse:
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


def _index_html() -> str:
    """把掛載前綴注入 index.html。

    主控台掛在 ROS 同網域的子路徑（/security）時，前端打 `/api/...` 會落到
    ROS 自己的路由上，必須帶前綴。前綴只有後端知道（來自設定檔），因此在
    這裡注入而不是寫死在前端。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # 只有接上 ROS（＝真的掛在它的子路徑下）才需要前綴；本機直接跑在
    # 127.0.0.1:8600 時沒有 reverse proxy，加了前綴反而會 404。
    mount = ""
    if ros.enabled():
        mount = str(settings().get("ros", {}).get("mount_path", "") or "").rstrip("/")
    return html.replace('window.__MOUNT__ = "";', f'window.__MOUNT__ = "{mount}";')


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
