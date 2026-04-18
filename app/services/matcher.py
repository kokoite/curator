"""Rule-based field matcher. No LLM, no I/O, pure stdlib + Pydantic."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.models.schemas import FormField, UserProfile


class MatchResult(BaseModel):
    profile_key: str
    value: Any | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_option_match: bool = False


# ── Rule tables ──────────────────────────────────────────────────────────

# (compiled regex on label, profile attribute, confidence)
# Order matters: first match wins. More specific patterns come first.
RULES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"^(first|given)\s*name$"), "first_name", 0.98),
    (re.compile(r"^(last|family|sur)\s*name$"), "last_name", 0.98),
    (re.compile(r"^full\s*name$"), "_full_name", 0.90),
    (re.compile(r"^e[-\s]?mail(\s*address)?$"), "email", 0.99),
    (re.compile(r"^(phone|mobile|cell)(\s*(number|phone))?$"), "phone", 0.95),
    (re.compile(r"^address(\s*line\s*1|\s*1)?$"), "address_line_1", 0.90),
    (re.compile(r"^(city|town)$"), "city", 0.95),
    (re.compile(r"^(state|province|region)$"), "state", 0.90),
    (re.compile(r"^(zip|postal)(\s*code)?$"), "postal_code", 0.95),
    (re.compile(r"^country$"), "country", 0.95),
    (re.compile(r"^linkedin"), "linkedin_url", 0.95),
    (re.compile(r"(portfolio|website|personal\s*site)"), "portfolio_url", 0.85),
    (re.compile(r"^github"), "github_url", 0.95),
    (re.compile(r"^(current|present)\s*(company|employer|organization)"), "current_company", 0.90),
    (re.compile(r"^(current|present)\s*(title|position|role|job)"), "current_title", 0.90),
    (re.compile(r"^years?\s*of\s*experience"), "years_of_experience", 0.90),
    (re.compile(r"^(expected|desired)\s*(salary|compensation)"), "salary_expectation", 0.85),
]

# Yes/no style rules — always need option matching because wording varies.
YES_NO_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(authorized|eligible|right)\s*to\s*work"), "work_authorized"),
    (re.compile(r"(require|need|visa).{0,30}(sponsor|sponsorship)"), "requires_sponsorship"),
    (re.compile(r"over\s*(the\s*)?age\s*of\s*18"), "_age_over_18"),
]

# ── Account-creation field rules ─────────────────────────────────────────
# Password and confirm-password both map to "password" (same generated value).
# The scraper synthesizes a UserProfile-like object with a `password` attribute
# so these fields are resolved by the standard fill pipeline.
ACCOUNT_RULES: list[tuple[re.Pattern[str], str | None, float]] = [
    (re.compile(r"^password$"), "password", 0.98),
    (re.compile(r"(confirm|verify|re-?enter)\s*password"), "password", 0.95),
    (re.compile(r"create\s*password"), "password", 0.95),
    (re.compile(r"security\s*question"), None, 0.0),  # flag for LLM or user
]

# Field types where the value must be selected from options.
_OPTION_TYPES = {"select", "radio_group", "combobox"}


# ── Helpers ──────────────────────────────────────────────────────────────

def _resolve_value(profile: UserProfile, key: str) -> Any | None:
    """Look up a profile key, handling composite/special keys."""
    if key == "_full_name":
        return f"{profile.first_name} {profile.last_name}"
    if key == "_age_over_18":
        return True
    return getattr(profile, key, None)


# ── Public API ───────────────────────────────────────────────────────────

def match_field(field: FormField, profile: UserProfile) -> MatchResult | None:
    """Return a direct match if rules are confident, else None (escalate to LLM)."""
    label = field.label.strip().lower()

    # Try yes/no rules first — these always need option matching.
    for pattern, key in YES_NO_RULES:
        if pattern.search(label):
            value = _resolve_value(profile, key)
            return MatchResult(
                profile_key=key,
                value=value,
                confidence=0.90,
                needs_option_match=True,
            )

    # Try standard rules.
    for pattern, key, confidence in RULES:
        if pattern.search(label):
            value = _resolve_value(profile, key)

            # For option-based fields, signal that the caller needs to
            # match the value against the available options.
            if field.type in _OPTION_TYPES:
                return MatchResult(
                    profile_key=key,
                    value=value,
                    confidence=confidence,
                    needs_option_match=True,
                )

            return MatchResult(
                profile_key=key,
                value=value,
                confidence=confidence,
                needs_option_match=False,
            )

    # Try account-creation rules (password / security question).
    # For password fields, the value is resolved from the profile's `password`
    # attribute (set on the synthetic profile the scraper builds).
    # For security questions (key=None), return a zero-confidence match so
    # the caller escalates to LLM or flags for user input.
    for pattern, key, confidence in ACCOUNT_RULES:
        if pattern.search(label):
            if key is None:
                return None  # escalate to LLM
            value = _resolve_value(profile, key)
            return MatchResult(
                profile_key=key,
                value=value,
                confidence=confidence,
                needs_option_match=False,
            )

    return None
