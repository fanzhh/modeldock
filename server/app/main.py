"""ModelDock server 入口。

启动即校验：只监听 127.0.0.1；allow_remote=1 时打印显式警告。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api import accounts as accounts_api
from app.api import chat as chat_api
from app.api import gen as gen_api
from app.api import models_api, recipes
from app.config import settings
from app.core.builtin_recipes import seed_builtin_recipes
from app.core.colab.autoreleaser import AutoReleaser
from app.core.colab.orchestrator import get_orchestrator
from app.core.drive import Drive
from app.core.downloads import engine as dl_engine_mod
from app.core.downloads.engine import DownloadEngine
from app.db import init_db
from app.schemas import HealthOut

log = logging.getLogger("modeldock")

drive = Drive()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_builtin_recipes()
    engine_dl = DownloadEngine(vault_getter=accounts_api.token_for, drive=drive)
    dl_engine_mod.engine_singleton = engine_dl  # 供 API 路由按需取用
    await engine_dl.start()
    app.state.download_engine = engine_dl
    autoreleaser = AutoReleaser(get_orchestrator().release, interval=30.0)
    await autoreleaser.start()
    app.state.autoreleaser = autoreleaser
    log.info("ModelDock ready at %s:%s (db=%s)", settings.host, settings.port, settings.db_path)
    yield
    await autoreleaser.stop()
    await engine_dl.stop()


app = FastAPI(title="ModelDock", version="0.1.0", lifespan=lifespan)

app.include_router(accounts_api.router)
app.include_router(models_api.router)
app.include_router(recipes.router)
app.include_router(chat_api.router)
app.include_router(gen_api.router)

# ---- web 控制台静态托管（npm run build 后无需另起 dev server）----
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.responses import FileResponse  # noqa: E402

_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_dist / "index.html")


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(ok=True, drive=drive.health(), version=app.version)  # type: ignore[attr-defined]


def main() -> None:
    import uvicorn

    if settings.allow_remote:
        log.warning(
            "MODELDOCK_ALLOW_REMOTE=1 — 服务将监听 %s。凭证保险箱与本服务同机，"
            "确认网络环境可信后再暴露端口。", settings.host,
        )
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
