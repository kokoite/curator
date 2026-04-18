"""POST /fill endpoint — accepts a Workday URL, returns filled form fields."""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException

from app.fixtures.dummy_credentials import get_dummy_credentials, save_dummy_credentials
from app.fixtures.dummy_profile import DUMMY_PROFILE
from app.models.schemas import FillRequest, FillResponse
from app.services.filler import fill_form
from app.services.scraper import extract_tenant, scraper

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

    # Resolve credentials: client-provided → dummy store → None
    tenant = extract_tenant(req.url)
    creds = req.known_credentials or (get_dummy_credentials(tenant) if tenant else None)

    # Scrape the form, forwarding credentials if available
    try:
        schema, account_action = await scraper.scrape(
            req.url, known_credentials=creds
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Scraper error: {exc}") from exc

    # Map scraper status to HTTP errors
    status_map = {
        "login_required": (409, "Login required"),
        "job_closed": (410, "Job posting is closed"),
        "no_form_found": (422, "No application form found"),
        "unsupported_flow": (422, "Unsupported application flow"),
        "invalid_credentials": (401, "Invalid credentials"),
        "account_creation_failed": (422, "Account creation failed"),
        "email_verification_required": (202, "Email verification required"),
    }

    # Statuses that proceed to form filling
    _OK_STATUSES = {"ok", "account_created", "signed_in"}

    if schema.status not in _OK_STATUSES:
        code, msg = status_map.get(schema.status, (422, schema.status))

        # email_verification_required is non-error — return partial response
        if schema.status == "email_verification_required":
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return FillResponse(
                job=schema.job,
                filled=[],
                unfilled=[],
                elapsed_ms=elapsed_ms,
                account_action=account_action,
            )

        raise HTTPException(status_code=code, detail=msg)

    # If scraper created a new account, persist credentials in dummy store
    if account_action.action == "created" and account_action.credentials:
        save_dummy_credentials(account_action.credentials)

    # Orchestrate filling using DUMMY_PROFILE
    filled, unfilled = await fill_form(schema, DUMMY_PROFILE)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return FillResponse(
        job=schema.job,
        filled=filled,
        unfilled=unfilled,
        elapsed_ms=elapsed_ms,
        account_action=account_action,
    )
