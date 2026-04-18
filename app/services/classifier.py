"""LLM classifier — maps a single form field to a profile value via Gemma 4 31B.

One field per call. No batching. No shared state between calls.
Pydantic validation + one-retry wrapper on every response.
"""

from __future__ import annotations

import json
import re

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.models.schemas import FormField, UserProfile

# ---------------------------------------------------------------------------
# ClassifyResult — local Pydantic model (NOT in schemas.py per spec)
# ---------------------------------------------------------------------------

class ClassifyResult(BaseModel):
    profile_key: str | None = None
    value: str | None = None
    confidence: float
    reason: str


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """\
You are classifying a single form field on a job application.
Given the field's label, type, and available options, AND the user's profile,
return the single best value to fill.

Output STRICT JSON:
{
  "profile_key": "<canonical key>" | null,
  "value": "<string matching an option exactly, OR a plain value>" | null,
  "confidence": 0.0-1.0,
  "reason": "<one short sentence>"
}

Rules:
- For type=select or radio_group: value MUST exactly match one of the option \
labels provided. If none fit, value=null, confidence<=0.4.
- For type=combobox: options are not listed here. Return the DESIRED label as \
plain text; the client will open the dropdown and fuzzy-match.
- For type=textarea with an open-ended question: return value=null, \
confidence=1.0, reason="free-text question" — generation handled elsewhere.
- Never invent profile data. If the profile lacks info, value=null."""

# JSON schema hint for Gemini's response_schema (Gemma treats as advisory)
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "profile_key": {"type": "string", "nullable": True},
        "value": {"type": "string", "nullable": True},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["confidence", "reason"],
}

# ---------------------------------------------------------------------------
# Lazy client
# ---------------------------------------------------------------------------

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# ---------------------------------------------------------------------------
# Minimal profile builder — omit resume_text and low-signal fields
# ---------------------------------------------------------------------------

_PROFILE_KEYS = [
    "first_name", "last_name", "email", "phone",
    "address_line_1", "city", "state", "postal_code", "country",
    "linkedin_url", "portfolio_url", "github_url",
    "work_authorized", "requires_sponsorship",
    "salary_expectation", "salary_currency",
    "years_of_experience", "current_company", "current_title",
    "skills",
]


def _minimal_profile(profile: UserProfile) -> dict:
    """Return only non-None profile keys relevant to classification."""
    data = profile.model_dump(include=set(_PROFILE_KEYS))
    return {k: v for k, v in data.items() if v is not None and v != [] and v != ""}


# ---------------------------------------------------------------------------
# User prompt builder
# ---------------------------------------------------------------------------

def _build_user_prompt(field: FormField, profile: UserProfile, extra: str = "") -> str:
    options_str = ""
    if field.options:
        labels = [o.label for o in field.options]
        options_str = f"\n  options: {json.dumps(labels)}"

    placeholder_str = ""
    if field.placeholder:
        placeholder_str = f"\n  placeholder: \"{field.placeholder}\""

    profile_json = json.dumps(_minimal_profile(profile), indent=2)

    return (
        f"FIELD:\n"
        f"  label: \"{field.label}\"\n"
        f"  type: \"{field.type}\"\n"
        f"  required: {field.required}"
        f"{options_str}"
        f"{placeholder_str}\n\n"
        f"PROFILE:\n{profile_json}"
        f"{extra}"
    )


# ---------------------------------------------------------------------------
# Raw LLM call
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


async def _call_llm(field: FormField, profile: UserProfile, extra: str = "") -> str:
    """Single LLM call. Returns raw text from the model."""
    client = _get_client()
    user_prompt = _build_user_prompt(field, profile, extra=extra)

    response = await client.aio.models.generate_content(
        model=settings.classifier_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=CLASSIFY_SYSTEM,
            response_mime_type="application/json",
            response_schema=CLASSIFY_SCHEMA,
            temperature=0.1,
        ),
    )
    return response.text or ""


# ---------------------------------------------------------------------------
# Public API — validate + retry wrapper
# ---------------------------------------------------------------------------

async def classify_field(
    field: FormField,
    profile: UserProfile,
    max_retries: int = 1,
) -> ClassifyResult:
    """Classify a single form field against a user profile.

    Uses Pydantic validation with a one-retry corrective nudge.
    On exhausted retries, returns confidence=0.0 with error reason
    rather than raising.
    """
    last_err: str | None = None

    for attempt in range(max_retries + 1):
        nudge = ""
        if last_err:
            nudge = (
                f"\n\nYour previous response was invalid: {last_err}. "
                "Output ONLY valid JSON matching the schema, nothing else."
            )

        raw = await _call_llm(field, profile, extra=nudge)

        try:
            # Strip markdown fences if the model wrapped the output
            cleaned = raw.strip()
            fence_match = _FENCE_RE.match(cleaned)
            if fence_match:
                cleaned = fence_match.group(1).strip()
            else:
                cleaned = (
                    cleaned
                    .removeprefix("```json")
                    .removeprefix("```")
                    .removesuffix("```")
                    .strip()
                )

            return ClassifyResult.model_validate_json(cleaned)
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
            if attempt == max_retries:
                return ClassifyResult(
                    profile_key=None,
                    value=None,
                    confidence=0.0,
                    reason=f"parse error after {max_retries + 1} attempts: {last_err}",
                )

    # Unreachable, but satisfies type checker
    return ClassifyResult(
        profile_key=None, value=None, confidence=0.0, reason="unreachable"
    )
