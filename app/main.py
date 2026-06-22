import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

# Surface application logs (logger.info/warning/...) to stdout. Without this
# only uvicorn's access logs appear and app-level warnings vanish, making
# issues like Bedrock/SQL failures invisible.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler
from sqlalchemy import text

from app.api.events import redis_event_listener
from app.api.v1.router import api_router
from app.api.v1.routes.websocket import router as ws_router
from app.core.config import INSECURE_DEFAULT_SECRET, settings
from app.core.limiter import limiter
from app.db.base import Base, async_engine

from app.models import user, organization, workflow_run, remediation

def _validate_security_config() -> None:
    """Refuse to boot a production instance with insecure defaults."""
    if settings.is_production and settings.SECRET_KEY == INSECURE_DEFAULT_SECRET:
        raise RuntimeError(
            "SECRET_KEY is set to the insecure development default while "
            "ENVIRONMENT is production. Set a strong SECRET_KEY before deploying."
        )

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Validate config and start the Redis event listener.

    Schema is NOT created here outside local development. In staging/
    production, schema changes happen exactly once via the Alembic
    `alembic upgrade head` Helm pre-upgrade hook job (see agora-helm's
    templates/migration-job.yaml), which runs to completion before the new
    Deployment is rolled out. Running create_all() on every pod start would
    race across replicas and bypass migration history entirely.
    """
    _validate_security_config()
    if settings.ENVIRONMENT == "development":
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    listener_task = asyncio.create_task(redis_event_listener())

    yield

    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
    await async_engine.dispose()

app = FastAPI(
    title="PipelineIQ API",
    version="0.1.0",
    description="AI-powered GitHub Actions remediation platform",
    lifespan=lifespan,
    # Swagger/ReDoc/OpenAPI schema are dev-only — don't expose the API's
    # internal shape publicly once deployed to production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

app.include_router(ws_router)

@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "api-service"}


@app.get("/health/live", tags=["health"])
async def liveness() -> dict:
    """Process is up and serving requests. No dependency checks."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness() -> JSONResponse:
    """Pod is ready to receive traffic — verifies DB and Redis are reachable."""
    checks: dict[str, str] = {}

    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    try:
        redis = await Redis.from_url(settings.REDIS_URL)
        await redis.ping()
        await redis.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
