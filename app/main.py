from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import get_settings
from app.logging import configure_logging

configure_logging()
settings = get_settings()
static_dir = Path(__file__).parent / "static"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "Локальная платформа сборки и запуска доверенных публичных GitHub-репозиториев. "
        "Worker имеет полный доступ к Docker Engine — не запускайте непроверенный код."
    ),
)
app.include_router(router)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False, response_class=FileResponse)
async def dashboard() -> FileResponse:
    return FileResponse(static_dir / "index.html")
