"""POST /fill endpoint — accepts a Workday URL, returns filled form fields."""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException

from app.fixtures.dummy_profile import DUMMY_PROFILE
from app.models.schemas import FillRequest, FillResponse
from app.services.filler import fill_form
from app.services.scraper import scraper

router = APIRouter()

_WORKDAY_URL_RE = re.compile(
    r"https?://[^/]*\.myworkday(jobs)?\.com/", re.IGNORECASE
)


@router.post("/fill", response_model=FillResponse)
async def fill(req: FillRequest) -> FillResponse:
    """Scrape a Workday job URL and return auto-filled form fields."""

    # Validate Workday URL
    if not _WORKDAY_URL_RE.search(req.url):
        raise HTTPException(status_code=400, detail="URL is not a Workday application page")

    start = time.monotonic()

    # Scrape the form
    try:
        schema = await scraper.scrape(req.url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Scraper error: {exc}") from exc

    # Map scraper status to HTTP errors
    status_map = {
        "login_required": (409, "Login required"),
        "job_closed": (410, "Job posting is closed"),
        "no_form_found": (422, "No application form found"),
        "unsupported_flow": (422, "Unsupported application flow"),
    }
    if schema.status != "ok":
        code, msg = status_map.get(schema.status, (422, schema.status))
        raise HTTPException(status_code=code, detail=msg)

    # Orchestrate filling using DUMMY_PROFILE
    filled, unfilled = await fill_form(schema, DUMMY_PROFILE)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return FillResponse(
        job=schema.job,
        filled=filled,
        unfilled=unfilled,
        elapsed_ms=elapsed_ms,
    )
