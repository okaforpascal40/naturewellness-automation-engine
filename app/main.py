from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import automation, manual_review

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("NatureWellness Automation Engine starting up…")
    yield
    logger.info("NatureWellness Automation Engine shut down.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="NatureWellness Automation Engine",
        description=(
            "Bioactive food recommendation system that maps diseases to foods "
            "via Disease → Gene → Pathway → Compound → Food evidence chains."
        ),
        version="1.0.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(automation.router, prefix="/api/v1")
    app.include_router(manual_review.router, prefix="/api/v1")

    return app


app = create_app()


@app.get("/health", tags=["health"], summary="Health check")
async def health() -> dict:
    return {"status": "ok", "service": "naturewellness-automation-engine"}


@app.get("/", tags=["root"], include_in_schema=False)
async def root() -> dict:
    return {"message": "NatureWellness Automation Engine", "docs": "/api/v1/docs"}
