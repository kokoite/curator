# Curator — Workday Application Agent

Automated job application form filler for Workday career sites. Given a Workday job URL, the agent scrapes the application form, matches fields to a user profile using rules + LLM, and returns structured fill instructions.

## Architecture

**Three-pass pipeline:**

1. **Rule-based matcher** — regex patterns map common field labels (name, email, phone, etc.) directly to profile attributes. No LLM call needed.
2. **LLM classifier** — fields the matcher can't handle are sent to Gemini (one call per field, parallel) to identify the best profile value. Textareas flagged as free-text are forwarded to pass 3.
3. **LLM generator** — open-ended questions (cover letter, "why this role?", etc.) get a first-person answer generated from the applicant's resume and the job description.

**Scraper** — Playwright-based headless browser navigates the Workday page, clicks through Apply flows, detects login walls / closed jobs, and extracts all form fields via in-page JS.

## API

### `GET /health`
Returns `{"status": "ok"}`.

### `POST /fill`
**Body:** `{"url": "<workday-job-url>"}`

**Response:** `FillResponse` containing `job` context, `filled` fields with selectors and values, `unfilled` fields that couldn't be resolved, and `elapsed_ms`.

**Error codes:**
- `400` — URL is not a Workday page
- `409` — Login required
- `410` — Job posting closed
- `422` — No form found / unsupported flow
- `502` — Scraper exception

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium

# Set your Gemini API key
export GEMINI_API_KEY=your-key-here

# Run the server
uvicorn app.main:app --reload
```

## Current Limitations (v1)

- Uses a hardcoded dummy profile (`app/fixtures/dummy_profile.py`). Profile CRUD is deferred to v2.
- No file upload support (resume attachment).
- Single-page form only — multi-step wizard support is partial.
