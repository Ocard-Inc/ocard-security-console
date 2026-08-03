"""沒有任何 API 端點可以是「async def + 阻塞查詢」。

守的是一個實際發生的故障：在 Log Explorer 查一個 API Log 的 IP（回看查詢跑滿
55 秒）期間，**整個主控台失去回應** —— 篩選、Controller 建議、什麼都出不來。
實測完全不碰 ClickHouse 的 `/api/session` 被拖到 53.6 秒，五分鐘排程也一起卡住。

原因：那些端點寫成 `async def`，於是阻塞的 ClickHouse／SQLite 呼叫跑在事件迴圈
上，所有請求排在後面。使用者看到的症狀不是「這個查詢很慢」，而是「全部壞掉」。

FastAPI 對同步 `def` 會丟進 threadpool，所以**同步才是這個專案的正解**
（`/sweep` 與 `/explorer/payload` 早就是同步的，並在註解裡寫明理由）。
`async def` 只有在函式體內真的有 `await` 時才成立。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1] / "src" / "console" / "api"

# app.py 的 lifespan / middleware / index / healthz 是框架要求的 async，
# 而且它們不做阻塞查詢（healthz 刻意不碰 DB）。
_FRAMEWORK_ASYNC = {"lifespan", "cache_policy", "index", "healthz"}


def _route_functions(path: Path):
    """(函式名, 是否 async, 是否含 await) —— 只看掛了 @router 裝飾器的。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name) and d.func.value.id in ("router", "app")
            for d in node.decorator_list)
        if not decorated:
            continue
        is_async = isinstance(node, ast.AsyncFunctionDef)
        has_await = any(isinstance(x, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                        for x in ast.walk(node))
        yield node.name, is_async, has_await


@pytest.mark.parametrize("filename", sorted(p.name for p in API_DIR.glob("*.py")))
def test_no_async_endpoint_without_await(filename):
    offenders = [
        name for name, is_async, has_await in _route_functions(API_DIR / filename)
        if is_async and not has_await and name not in _FRAMEWORK_ASYNC
    ]
    assert not offenders, (
        f"{filename} 有 async def 端點但函式體內沒有 await：{offenders}。\n"
        "裡面的查詢是阻塞的 → 會佔住事件迴圈，一個慢查詢讓整個主控台停止回應"
        "（連五分鐘排程一起卡住）。改成同步 def，FastAPI 會丟進 threadpool。")


def test_the_expensive_endpoints_are_definitely_sync():
    """幾個已知會跑很久的端點，逐一點名確認 —— 不依賴上面那條的推導。"""
    import inspect
    from console.api import routes
    for name in ("run_explorer", "overview", "event_detail", "event_entity",
                 "event_entity_timeline", "explorer_payload", "run_sweep",
                 "suggest_endpoints", "data_health"):
        fn = getattr(routes, name, None)
        assert fn is not None, f"routes.{name} 不存在（改名了？請更新這個清單）"
        assert not inspect.iscoroutinefunction(fn), \
            f"routes.{name} 是 async def —— 它會做阻塞查詢"
