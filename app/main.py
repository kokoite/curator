"""FastAPI application entrypoint with lifespan management."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.fill import router as fill_router
from app.services.scraper import scraper


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start Playwright scraper on startup, stop on shutdown."""
    await scraper.start()
    yield
    await scraper.stop()


app = FastAPI(
    title="Workday Application Agent",
    version="0.1.0",
    description="Automated Workday job application form filler powered by LLM + rule-based matching.",
    lifespan=lifespan,
)

app.include_router(fill_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
