"""FastAPI application factory with health checks and OpenAPI scopes."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from dataagent import __version__
from dataagent.api.routes import router
from dataagent.authorization.roles import SYSTEM_ROLES, role_payload
from dataagent.config import get_settings
from dataagent.storage.database import check_schema_compatible, init_engine, session_scope
from dataagent.storage import models as m
from dataagent.storage.models import DEFAULT_MODULES


def seed_system_roles(session) -> None:
    for name, role in SYSTEM_ROLES.items():
        existing = session.execute(select(m.Role).where(m.Role.name == name)).scalar_one_or_none()
        if existing is None:
            session.add(
                m.Role(
                    tenant_id=None,
                    name=name,
                    is_system=True,
                    actions=sorted(role.actions),
                    fields=sorted(role.fields),
                    delegable=role.delegable,
                )
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as session:
        seed_system_roles(session)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Data Agent API",
        version=__version__,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "agents", "description": "Agent catalog"},
            {"name": "ingestion", "description": "Events, OTLP, webhooks"},
            {"name": "analytics", "description": "Metrics and charts"},
            {"name": "benchmarks", "description": "QA, refresh, versions"},
            {"name": "experiments", "description": "Compare releases"},
            {"name": "access", "description": "Roles and memberships"},
        ],
    )
    app.include_router(router)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "unexpected error", "request_id": request_id},
        )

    @app.get("/healthz")
    def liveness():
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness():
        try:
            engine = init_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            with session_scope() as session:
                if not check_schema_compatible(session):
                    return JSONResponse(
                        status_code=503,
                        content={"status": "not_ready", "reason": "schema_missing"},
                    )
            return {"status": "ready", "version": __version__}
        except Exception as e:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": type(e).__name__},
            )

    @app.get("/v1/meta")
    def meta():
        return {
            "version": __version__,
            "auth_mode": settings.auth_mode,
            "system_roles": [role_payload(r) for r in SYSTEM_ROLES.values()],
            "default_modules": DEFAULT_MODULES,
        }

    return app
