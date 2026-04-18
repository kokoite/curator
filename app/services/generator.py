"""LLM free-text generator — produces first-person answers for open-ended
job application questions via Gemini 2.5 Pro.

One field per call. Returns plain text, no JSON.
"""

from __future__ import annotations

import json

from google import genai
from google.genai import types

from app.config import settings
from app.models.schemas import FormField, JobContext, UserProfile

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GENERATE_SYSTEM = """\
You are writing an answer to an open-ended job application question,
in first person, in the applicant's voice.

Rules:
- Be specific. Reference real projects/companies/achievements from the resume.
- Professional but human. Avoid corporate cliches.
- Respect max_length; aim comfortably under.
- Output just the answer text. No preamble, no quotes, no markdown."""

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
# User prompt builder
# ---------------------------------------------------------------------------

def _build_user_prompt(
    field: FormField,
    profile: UserProfile,
    job_context: JobContext,
) -> str:
    job_desc = (job_context.job_description or "")[:3000]

    # Prefer resume_text; fall back to compact profile JSON
    if profile.resume_text:
        applicant_section = profile.resume_text
    else:
        compact = profile.model_dump(exclude={"resume_text", "user_id"})
        compact = {k: v for k, v in compact.items() if v is not None and v != [] and v != ""}
        applicant_section = json.dumps(compact, indent=2)

    max_length = field.max_length or "aim 150-250 words"

    return (
        f"JOB:\n"
        f"  title: {job_context.job_title or 'N/A'}\n"
        f"  company: {job_context.company or 'N/A'}\n"
        f"  description: {job_desc}\n\n"
        f"APPLICANT RESUME:\n{applicant_section}\n\n"
        f"QUESTION:\n{field.label}\n\n"
        f"Max length: {max_length}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate(
    field: FormField,
    profile: UserProfile,
    job_context: JobContext,
) -> str:
    """Generate a free-text answer for an open-ended application question.

    Returns the stripped answer string. No JSON, no structured output.
    """
    client = _get_client()
    user_prompt = _build_user_prompt(field, profile, job_context)

    response = await client.aio.models.generate_content(
        model=settings.generator_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=GENERATE_SYSTEM,
            temperature=0.7,
            max_output_tokens=1024,
        ),
    )
    return (response.text or "").strip()
