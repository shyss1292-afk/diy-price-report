"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .config import APP_NAME, APP_VERSION, HOST, PORT, WEB_DIR
from .db import init_db, session_scope
from .scheduler import job_status, shutdown_scheduler, start_scheduler
from .services import trend as trend_svc
from .services.bootstrap import bootstrap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diyprice")

PAGES: dict[str, str] = {
    "/": "index.html",
    "/report": "report.html",
    "/products": "products.html",
    "/product": "product.html",
    "/compare": "compare.html",
    "/admin": "admin.html",
    # 装机助手（手机优先）—— 用 /build 而不是 /builds，页面名与 API 前缀分开，
    # 免得以后加二级路由时和 /api/builds 混淆。
    "/build": "build.html",
}


def create_app(enable_scheduler: bool | None = None) -> FastAPI:
    # 进程内调度器默认**关闭** —— 采集已交给 launchd（com.diyprice.collect）。
    # 改成 opt-in 而不是 opt-out：原来靠 plist 里的 DIYPRICE_SCHEDULER=0 关闭，
    # 实测这个环境变量没能可靠传到进程里，结果两套调度并存、任务重复执行。
    # 需要临时启用（例如本地调试）时设 DIYPRICE_SCHEDULER=1 即可。
    if enable_scheduler is None:
        enable_scheduler = os.environ.get("DIYPRICE_SCHEDULER", "0") == "1"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        with session_scope() as session:
            stats = bootstrap(session)
        logger.info(
            "%s v%s 就绪 | 新增平台 %s 个 / 新增型号 %s 个 | http://%s:%s",
            APP_NAME, APP_VERSION, stats["platforms_added"], stats["products_added"], HOST, PORT,
        )
        if enable_scheduler:
            start_scheduler()
        else:
            logger.info("调度器已禁用（DIYPRICE_SCHEDULER=0）")
        # 后台预热市场序列缓存：不阻塞启动，但让"重启后的第一个请求"也走缓存，
        # 否则首次打开首页要现场算整份序列（实测约 800ms）。
        threading.Thread(target=_warm_market_cache, name="warm-cache", daemon=True).start()
        yield
        if enable_scheduler:
            shutdown_scheduler()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def _no_stale_cache(request, call_next):
        """禁止浏览器对页面与静态资源做「启发式缓存」。

        Starlette 的 StaticFiles 只发 ETag / Last-Modified、**不发 Cache-Control**，
        浏览器于是按 Last-Modified 推算一个启发式保鲜期，直接拿本地旧副本用 ——
        表现就是改完前端刷新页面还是老样子（实测踩过：改了 index.js，
        页面仍发旧请求）。
        这里统一声明 no-cache：**允许缓存但每次必须回源校验**，
        文件没变时服务端回 304、几乎零成本，文件变了立刻生效。
        """
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path in PAGES:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    app.include_router(api_router)

    @app.get("/api/scheduler")
    def scheduler_info() -> dict:
        return {"jobs": job_status()}

    # 页面路由
    for route, filename in PAGES.items():
        app.get(route, include_in_schema=False)(_page_factory(filename))

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.exception_handler(404)
    async def not_found(_request, exc):  # pragma: no cover
        detail = getattr(exc, "detail", "资源不存在")
        return JSONResponse(status_code=404, content={"detail": detail})

    return app


def _warm_market_cache() -> None:
    """后台预热首页与日报要用的数据。失败不影响服务可用性。"""
    from .services import report as report_svc

    with session_scope() as session:
        try:
            trend_svc.warm_market_cache(session, days=180)
            logger.info("市场序列缓存预热完成")
        except Exception:  # noqa: BLE001
            logger.warning("市场序列缓存预热失败，首个请求将现场计算", exc_info=True)

        # 日报没有缓存层，首访要现场算一遍（实测约 0.5s）—— 一并预热，
        # 让「重启后第一次打开日报」也没有等待感。
        for category in report_svc.REPORT_CATEGORIES:
            try:
                report_svc.build_daily_report(session, category=category)
            except Exception:  # noqa: BLE001
                logger.warning("日报预热失败（%s）", category, exc_info=True)


def _page_factory(filename: str):
    def _page() -> FileResponse:
        path = WEB_DIR / filename
        if not path.exists():
            return JSONResponse(status_code=500, content={"detail": f"页面缺失：{filename}"})
        return FileResponse(path)

    _page.__name__ = f"page_{filename.replace('.', '_')}"
    return _page


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
