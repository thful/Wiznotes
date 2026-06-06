import asyncio
import logging
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，支持 python web/app.py 直接运行
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.config import (
    DEFAULT_ENV_PATH,
    DEFAULT_EXPORT_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_WORKERS,
    DEFAULT_MAX_NOTES,
    DEFAULT_REEXPORT_DOT_FILES,
    DEFAULT_EXCLUDE_FOLDERS,
)

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from web.websocket_manager import WebSocketManager
    from web.services.export_service import TaskManager
    from web.logging_handler import WebLogHandler

    ws_manager = WebSocketManager()
    ws_manager.set_loop(asyncio.get_event_loop())
    task_manager = TaskManager()
    app.state.ws_manager = ws_manager
    app.state.task_manager = task_manager

    # Install custom log handler
    handler = WebLogHandler(ws_manager, task_manager)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter('%(message)s'))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    app.state.log_handler = handler

    yield

    # Shutdown
    logging.getLogger().removeHandler(handler)


def create_app() -> FastAPI:
    app = FastAPI(title="WizNotes Exporter", docs_url="/docs", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # Runtime state
    app.state.wiz_client = None
    app.state.config = {
        "env_path": str(DEFAULT_ENV_PATH),
        "export_dir": str(DEFAULT_EXPORT_DIR),
        "log_dir": str(DEFAULT_LOG_DIR),
        "max_workers": DEFAULT_MAX_WORKERS,
        "max_notes": DEFAULT_MAX_NOTES,
        "reexport_dot_files": DEFAULT_REEXPORT_DOT_FILES,
        "exclude_folders": DEFAULT_EXCLUDE_FOLDERS,
    }

    from web.routers import auth, folders, export, stats

    app.include_router(auth.router)
    app.include_router(folders.router)
    app.include_router(export.router)
    app.include_router(stats.router)

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, reload=True)
