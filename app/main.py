from fastapi import FastAPI

from app.api.routes import router as health_router
from app.api.tenants import router as tenants_router
from app.api.workspaces import router as workspaces_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(health_router, prefix="/api")
app.include_router(tenants_router, prefix="/api")
app.include_router(workspaces_router, prefix="/api")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": settings.app_name, "env": settings.env}
