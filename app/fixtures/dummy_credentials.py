"""Hardcoded dummy credentials fixture for testing account creation flows."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from app.models.schemas import Credentials

_IST = timezone(timedelta(hours=5, minutes=30))

DUMMY_CREDENTIALS: dict[str, Credentials] = {
    "example-tenant.wd1.myworkdayjobs.com": Credentials(
        tenant="example-tenant.wd1.myworkdayjobs.com",
        email="priya.sharma@example.com",
        password="DummyP@ss2024Secure",
        created_at=datetime(2026, 1, 1, tzinfo=_IST),
        verified=True,
        source="created",
    ),
}


def get_dummy_credentials(tenant: str) -> Credentials | None:
    """Return stored credentials for a tenant, or None."""
    return DUMMY_CREDENTIALS.get(tenant)


def save_dummy_credentials(creds: Credentials) -> None:
    """Persist credentials in the in-memory store (keyed by tenant)."""
    DUMMY_CREDENTIALS[creds.tenant] = creds
