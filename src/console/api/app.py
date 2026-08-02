"""FastAPI 應用：Security Log Console 後端 + SPA 靜態檔。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from console.api import routes
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
async def no_cache_static(request: Request, call_next):
    """前端是無建置流程的 ES module，瀏覽器快取會讓改動不生效。"""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


def main() -> None:
    import uvicorn
    cfg = settings()["app"]
    uvicorn.run("console.api.app:app", host=cfg["host"], port=cfg["port"], reload=False)


if __name__ == "__main__":
    main()
