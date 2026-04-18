# Workday Application Agent — Build Spec

## Context

Backend service that takes a Workday job URL + a user profile, returns filled form field values. No frontend, no extension yet. Pure API.

**Core architectural principle:** LLM does as little as possible. Rule-based matching handles 70–80% of fields. LLM is called only for ambiguous cases and free-text generation, and always with a minimal, focused prompt — never a dump of the whole form.

**Why:** smaller prompts = less hallucination, lower cost, easier debugging, faster response times. We'd rather make 10 small LLM calls with 90% accuracy each than 1 big call with 60% accuracy.

---

## Stack

- **Python 3.11+** with **FastAPI** (async)
- **Playwright** (Chromium) for scraping
- **Google AI Studio** (`google-genai` SDK) — single API, multiple models
  - **Classifier:** `gemma-4-31b-it` primary (free tier, 14.4K RPD), `gemini-2.0-flash` fallback
  - **Generator:** `gemini-2.5-pro` (quality matters for free-text)
- **Pydantic v2** for schemas + output validation
- In-memory profile store (swap for Postgres later)

### Model choice rationale
- **Classifier** is a simple per-field task. Gemma 4 31B is free and capable enough. If its structured-output reliability disappoints at runtime, swap to `gemini-2.0-flash` via env var — no code changes.
- **Generator** writes recruiter-facing prose. Quality is the moat here. Worth paying Gemini Pro rates for ~1 call per application.
- Both models are accessed through the same `google-genai` SDK, same API key, same AI Studio quota dashboard.

---

## API Surface (v1 — Dummy Profile Phase)

**One real endpoint.** Profile CRUD is deferred. The backend uses a hardcoded fake profile from `app/fixtures/dummy_profile.py` for every request. This lets us validate scraping + matching + LLM layers end-to-end before building the data layer.

### `POST /fill`
- Request body: `{ "url": "<workday-url>" }`  ← no `user_id`
- Returns: `FillResponse` with per-field instructions

**HTTP status mapping:**
- 200 — `FillResponse`
- 400 — URL is not a Workday URL
- 409 — Workday requires sign-in before form (login wall)
- 410 — job is closed
- 422 — no form found after apply / unsupported flow
- 502 — Playwright / scrape failure

### `GET /health`
- Returns: `{"status": "ok"}`

### Phase 2 (future — NOT in this build)
When dummy-profile v1 is validated, add: `POST /profile`, `GET /profile/{user_id}`, `DELETE /profile/{user_id}`, and thread `user_id` through `/fill`. The `UserProfile` shape is already defined so this is a clean extension — swap the dummy import for a store lookup.

---

## Data Models

```python
# User profile (canonical shape)
UserProfile:
  user_id: str
  first_name, last_name, email: str
  phone, address_line_1, city, state, postal_code, country: str | None
  linkedin_url, portfolio_url, github_url: str | None
  work_authorized, requires_sponsorship: bool | None
  salary_expectation: int | None
  salary_currency: str = "USD"
  years_of_experience: int | None
  current_company, current_title: str | None
  work_history: list[WorkExperience]
  education: list[Education]
  skills: list[str]
  resume_text: str | None   # for free-text generation

# Scraped form field
FieldType = "text" | "email" | "phone" | "number" | "url"
          | "textarea" | "select" | "combobox" | "radio_group"
          | "checkbox" | "date" | "file" | "unknown"

InteractionMode = "type" | "select_native" | "combobox"
                | "radio" | "checkbox" | "file" | "date"

FieldOption:
  label: str            # visible text ("United States")
  value: str | None     # internal value
  selector: str | None  # CSS selector for this specific option (e.g. radio)

FormField:
  field_id: str         # "f_0001", assigned by scraper
  selector: str         # CSS selector for the interactive element
  label: str            # "First Name"
  type: FieldType
  interaction: InteractionMode
  required: bool
  max_length: int | None
  options: list[FieldOption]  # empty for combobox (resolved at fill time)
  placeholder: str | None
  automation_id: str | None   # raw data-automation-id, for debugging

ScrapeStatus = "ok" | "login_required" | "no_form_found"
             | "job_closed" | "unsupported_flow"

FormSchema:
  status: ScrapeStatus
  job: JobContext           # url, job_title, company, job_description
  fields: list[FormField]
  current_step: int | None  # e.g. 1 of 5
  total_steps: int | None
  diagnostics: dict[str, str]

# Output
FilledField:
  field_id: str
  selector: str
  label: str
  value: str | bool | list[str]
  interaction: InteractionMode
  option_selector: str | None  # for radio, the specific radio's selector
  confidence: float            # 0.0 – 1.0
  reason: str | None
  needs_review: bool           # true for free-text + low-confidence

FillRequest:
  url: HttpUrl                  # just the URL; no user_id in v1

FillResponse:
  job: JobContext
  filled: list[FilledField]
  unfilled: list[FormField]
  elapsed_ms: int
```

**v1 uses a hardcoded dummy profile** at `app/fixtures/dummy_profile.py`. The `UserProfile` model stays complete (it's the target shape for Phase 2), but v1 never stores or retrieves one from a user.

---

## Project Layout (v1)

```
workday-agent/
├── requirements.txt
├── .env.example
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + lifespan
│   ├── config.py            # env vars via pydantic-settings
│   ├── models/
│   │   └── schemas.py       # all pydantic models (full UserProfile shape)
│   ├── fixtures/
│   │   └── dummy_profile.py # ← hardcoded fake profile for v1
│   ├── routers/
│   │   └── fill.py          # POST /fill (only endpoint besides /health)
│   └── services/
│       ├── scraper.py       # Playwright scraper
│       ├── matcher.py       # RULE-BASED field matching (no LLM)
│       ├── classifier.py    # LLM classifier (ONE field at a time)
│       ├── generator.py     # LLM free-text generator (ONE field at a time)
│       └── filler.py        # orchestrator that uses the three above
└── scripts/
    └── test_scraper.py      # standalone scraper test (no LLM needed)
```

**Deferred to Phase 2:** `app/services/profile_store.py`, `app/routers/profile.py`. The `UserProfile` Pydantic model in `schemas.py` is complete so Phase 2 is additive-only.

---

## Scraper Design (`app/services/scraper.py`)

### Responsibilities
1. Navigate to Workday URL
2. Wait for React hydration (`networkidle` + explicit wait for form fields)
3. Detect and click "Apply" → "Apply Manually" if on job posting page
4. Detect terminal states before scraping: login wall, job closed
5. Extract `FormSchema` from the rendered form
6. Dump HTML + screenshot to `/tmp/workday-dumps/` on any non-ok status

### Pattern-based detection (do NOT hardcode exact `data-automation-id` values)

Workday automation-ids vary across tenants and versions. Use regex patterns:

```python
_AID_APPLY_BUTTON = r"(apply|adventure).*button"
_AID_APPLY_MANUALLY = r"applyManually|manualApplication"
_AID_JOB_HEADER = r"jobPostingHeader|jobTitle"
_AID_JOB_DESCRIPTION = r"jobPostingDescription|jobDescription"
_AID_STEP_INDICATOR = r"progressBar|stepIndicator|wizardStep"
```

Always have a **text-based fallback** — if the regex misses, try `page.get_by_role("button", name="Apply")` etc. Record which strategy fired in `diagnostics`.

### Login wall detection
Count signals in page text — need ≥2 hits to confirm (a single "Sign In" link doesn't count):
```
["Create Account", "Sign In", "Already have an account",
 "Welcome back", "Returning User", "New User"]
```

### Field extraction (in-browser via `page.evaluate`)

All extraction happens inside one JS function executed in the browser — faster and avoids round-trips.

**Handle each field type:**

1. **Native inputs** (`<input>` excluding hidden/submit/button/radio):
   - Map `type` attribute → FieldType + InteractionMode
   - Grab label via: `aria-label` → `aria-labelledby` → `<label for>` → ancestor `data-automation-id` wrapper

2. **Textareas**: same label-resolution strategy. Always `interaction: "type"`.

3. **Native `<select>`**: enumerate options with label+value. `interaction: "select_native"`.

4. **ARIA comboboxes** (Workday's default dropdown — NOT a `<select>`):
   - Targets: `[role="combobox"]`, `button[aria-haspopup="listbox"]`, `[aria-haspopup="listbox"]`
   - Record the trigger selector only. Options are portal-rendered when open, so they're resolved at FILL TIME by the client, not at scrape time.
   - `interaction: "combobox"`, `options: []`

5. **Radio groups**: group by `name` attribute. Each option carries its own selector. Group label comes from `<legend>` or ancestor `[role="radiogroup"]` / `[data-automation-id]` block.

6. **Checkboxes**: individual fields, not grouped.

7. **Dedup**: seen-set by `selector + label` — Workday sometimes duplicates wrappers.

### Label resolution (4-level fallback)
```
1. aria-label
2. aria-labelledby (resolve IDs, join text)
3. <label for="{el.id}">
4. walk up ancestor data-automation-id / data-fkit-id blocks; look for
   <label>, [role="label"], .gwt-Label
```

### Selector resolution (priority order)
```
1. [data-automation-id='<aid>']   # most stable
2. #<escaped-id>
3. [name='<name>']
```

### Waiting for form readiness
```js
() => {
  const hasNative = document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select'
  ).length > 0;
  const hasCombobox = document.querySelectorAll(
    '[role="combobox"], button[aria-haspopup="listbox"]'
  ).length > 0;
  return hasNative || hasCombobox;
}
```

### Step indicator extraction
- Find element matching `_AID_STEP_INDICATOR` regex
- Try to parse "Step X of Y" text; fall back to counting `[role="listitem"]` children with `aria-current="step"`

### Failure diagnostics
On any non-ok status, save both HTML and full-page screenshot to `/tmp/workday-dumps/{timestamp}_{status}_{url-slug}.{html,png}`. Never let this throw — wrap in try/except.

---

## Dummy Profile Fixture — `app/fixtures/dummy_profile.py`

**v1 has no user onboarding.** Every `/fill` request uses the same hardcoded fake profile. This file contains:

```python
"""
Dummy profile for v1. A realistic-looking fake applicant used to validate
the end-to-end pipeline before real profile storage is built.

To swap for a real profile in Phase 2, replace this import in filler.py with
a store.get(user_id) lookup.
"""
from app.models.schemas import UserProfile, WorkExperience, Education


DUMMY_PROFILE = UserProfile(
    user_id="dummy",
    first_name="Alex",
    last_name="Rivera",
    email="alex.rivera.test@example.com",
    phone="+1-415-555-0182",
    address_line_1="742 Evergreen Terrace",
    city="San Francisco",
    state="CA",
    postal_code="94110",
    country="United States",
    linkedin_url="https://linkedin.com/in/alex-rivera-fake",
    portfolio_url="https://alexrivera.dev",
    github_url="https://github.com/alexrivera-fake",
    work_authorized=True,
    requires_sponsorship=False,
    salary_expectation=165000,
    salary_currency="USD",
    years_of_experience=7,
    current_company="Northwind Systems",
    current_title="Senior Software Engineer",
    work_history=[
        WorkExperience(
            company="Northwind Systems",
            title="Senior Software Engineer",
            start_date="2022-03",
            end_date=None,
            description=(
                "Lead engineer on distributed data pipelines processing 2B+ events/day. "
                "Drove migration from monolith to event-driven services, reducing p99 "
                "latency by 40%. Mentor to three junior engineers."
            ),
        ),
        WorkExperience(
            company="Lumenstack",
            title="Software Engineer",
            start_date="2019-06",
            end_date="2022-02",
            description=(
                "Built the internal experimentation platform used by 60+ product teams. "
                "Shipped the Python SDK, sample-size calculator, and the first version "
                "of the Bayesian analysis engine."
            ),
        ),
        WorkExperience(
            company="Helio Labs",
            title="Junior Engineer (intern → full-time)",
            start_date="2017-09",
            end_date="2019-05",
            description=(
                "Full-stack work on the customer dashboard: React + FastAPI + Postgres. "
                "Owned the billing integration with Stripe."
            ),
        ),
    ],
    education=[
        Education(
            institution="University of California, Davis",
            degree="B.S.",
            field_of_study="Computer Science",
            graduation_year=2017,
        ),
    ],
    skills=[
        "Python", "Go", "TypeScript", "React", "FastAPI", "Postgres",
        "Kafka", "AWS", "Kubernetes", "Distributed systems", "Bayesian stats",
    ],
    resume_text=(
        "ALEX RIVERA\n"
        "Senior Software Engineer — San Francisco, CA\n"
        "alex.rivera.test@example.com · linkedin.com/in/alex-rivera-fake · github.com/alexrivera-fake\n\n"
        "EXPERIENCE\n\n"
        "Northwind Systems — Senior Software Engineer (Mar 2022 – Present)\n"
        "• Lead engineer on distributed data pipelines processing 2B+ events/day.\n"
        "• Drove migration from monolith to event-driven services; p99 latency dropped 40%.\n"
        "• Designed the retry + dead-letter framework now used across 12 services.\n"
        "• Mentor to three junior engineers; run the weekly systems-design study group.\n\n"
        "Lumenstack — Software Engineer (Jun 2019 – Feb 2022)\n"
        "• Built the internal experimentation platform used by 60+ product teams.\n"
        "• Shipped the Python SDK (12K internal installs) and sample-size calculator.\n"
        "• First version of the Bayesian analysis engine — reduced required sample sizes 30%.\n\n"
        "Helio Labs — Junior Engineer, then Engineer (Sep 2017 – May 2019)\n"
        "• Full-stack React + FastAPI + Postgres on the customer dashboard.\n"
        "• Owned the Stripe billing integration through a migration to usage-based pricing.\n\n"
        "EDUCATION\n"
        "University of California, Davis — B.S. Computer Science, 2017\n\n"
        "SKILLS\n"
        "Python · Go · TypeScript · React · FastAPI · Postgres · Kafka · AWS · Kubernetes · "
        "Distributed systems · Bayesian statistics\n"
    ),
)
```

**Why a realistic fake:** the free-text generator needs substance to write convincing answers. An empty or generic profile produces garbage output and makes it impossible to evaluate whether the generator is working. A rich fake resume lets you judge quality on real-looking prompts ("Tell us about a challenging project you led") even with zero real users.

---

## Matcher (Rule-Based, No LLM) — `app/services/matcher.py`

This is the new piece. **Handle the obvious cases without touching an LLM.**

### Interface
```python
def match_field(field: FormField, profile: UserProfile) -> MatchResult | None:
    """Return direct match if rules are confident, else None (escalate to LLM)."""
```

### Rules (keyword-based, case-insensitive label matching)

```python
RULES = [
  # (regex pattern for label, profile attr, confidence)
  (r"^(first|given)\s*name$", "first_name", 0.98),
  (r"^(last|family|sur)\s*name$", "last_name", 0.98),
  (r"^full\s*name$", "<composite: first + last>", 0.90),
  (r"^e[-\s]?mail( address)?$", "email", 0.99),
  (r"^(phone|mobile|cell)( number)?$", "phone", 0.95),
  (r"^address( line 1| 1)?$", "address_line_1", 0.90),
  (r"^(city|town)$", "city", 0.95),
  (r"^(state|province|region)$", "state", 0.90),
  (r"^(zip|postal)( code)?$", "postal_code", 0.95),
  (r"^country$", "country", 0.95),
  (r"linkedin", "linkedin_url", 0.95),
  (r"(portfolio|website|personal\s*site)", "portfolio_url", 0.85),
  (r"github", "github_url", 0.95),
  (r"(current|present)\s*(company|employer|organization)", "current_company", 0.90),
  (r"(current|present)\s*(title|position|role|job)", "current_title", 0.90),
  (r"years?\s*of\s*experience", "years_of_experience", 0.90),
  (r"(expected|desired)\s*(salary|compensation)", "salary_expectation", 0.85),
]
```

### How matching works
For each field:
1. Normalize label (lowercase, trim)
2. Try each rule in order; first match wins
3. For `text` / `email` / `phone` / `number` / `url` types, use the matched profile value directly
4. For `select` / `radio_group` / `combobox` — rules tell us *what the value means semantically* but the chosen option still has to match available options. Matcher returns `profile_key` + `intended_value`; the caller decides whether to use it directly or escalate to the LLM to pick an option.
5. If NO rule matches, return `None` — LLM will handle.

### Special rule: work authorization / sponsorship
These almost always show up as radio/combobox with yes/no-style options. Handle as a second-tier rule:
```python
YES_NO_RULES = [
  (r"(authorized|eligible|right)\s*to\s*work", "work_authorized"),
  (r"(require|need|visa).{0,30}(sponsor|sponsorship)", "requires_sponsorship"),
  (r"over\s*(the\s*)?age\s*of\s*18", "<always true>"),
]
```
These still need option-matching (match `True`/`False` to one of the visible option labels), which escalates to the LLM because option wording varies wildly.

### Output
```python
MatchResult:
  profile_key: str
  value: str | bool | int | None       # direct value if mappable
  confidence: float
  needs_option_match: bool             # true if select/radio/combobox — LLM picks the option
```

---

## Classifier (LLM, ONE field at a time) — `app/services/classifier.py`

**Critical:** process fields one at a time (or small batches of 3–5 for simple types). Do NOT send the whole form in one prompt. This is the anti-hallucination discipline.

### When it's called
Only when:
- `matcher.match_field()` returned `None` (no rule matched), OR
- Matcher returned `needs_option_match=True` (need to pick from specific options)

### Prompt shape (tiny, focused)

```
SYSTEM:
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
- For type=select or radio_group: value MUST exactly match one of the option
  labels provided. If none fit, value=null, confidence<=0.4.
- For type=combobox: options are not listed here. Return the DESIRED label as
  plain text; the client will open the dropdown and fuzzy-match.
- For type=textarea with an open-ended question: return value=null,
  confidence=1.0, reason="free-text question" — generation handled elsewhere.
- Never invent profile data. If the profile lacks info, value=null.

USER:
FIELD:
  label: "{field.label}"
  type: "{field.type}"
  required: {field.required}
  options: {field.options | label-only list, if present}

PROFILE:
{minimal profile JSON — only the keys likely to be relevant: contact,
 address, work auth, experience. Omit `resume_text`.}
```

### Model
Primary: `GEMMA_MODEL` (default `gemma-4-31b-it`) — free tier, 14.4K RPD.
Fallback: `GEMINI_MODEL_FAST` (default `gemini-2.0-flash`) — if Gemma's structured output proves unreliable at runtime.

Both use the same `google-genai` SDK:
```python
response = await client.aio.models.generate_content(
    model=settings.classifier_model,
    contents=prompt,
    config=types.GenerateContentConfig(
        system_instruction=CLASSIFY_SYSTEM,
        response_mime_type="application/json",
        response_schema=CLASSIFY_SCHEMA,  # Gemini enforces strictly; Gemma loosely
        temperature=0.1,
    ),
)
```

### Output validation + one-retry (MANDATORY, regardless of model)

Gemma models typically honor `response_mime_type="application/json"` but treat
`response_schema` as a hint, not a hard constraint. Even Gemini occasionally
returns malformed JSON. Wrap every classifier call in a validate-and-retry
layer — this is cheap insurance and lets you swap models freely.

```python
from pydantic import BaseModel, ValidationError

class ClassifyResult(BaseModel):
    profile_key: str | None = None
    value: str | None = None
    confidence: float
    reason: str

async def classify_field(field, profile, max_retries: int = 1) -> ClassifyResult:
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
            # Strip ```json fences if the model wrapped the output
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return ClassifyResult.model_validate_json(cleaned)
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
            if attempt == max_retries:
                # Final fallback: mark as unfillable rather than crash the batch
                return ClassifyResult(
                    profile_key=None, value=None, confidence=0.0,
                    reason=f"parse error after {max_retries + 1} attempts: {last_err}"
                )
    # unreachable, but satisfies type checker
    return ClassifyResult(profile_key=None, value=None, confidence=0.0, reason="unreachable")
```

**Why this matters:** one bad LLM response shouldn't kill the whole form fill.
The retry gives the model a second chance with a corrective nudge; if that
still fails, the field goes to `unfilled` and the user handles it manually.
No crashes, no half-filled forms.

### Batching (optional, conservative)
If you want to batch for performance, batch ONLY fields of the same type and ONLY up to 5 at a time. The prompt becomes "classify each of these 5 similar fields" — still focused, still small context. If you're unsure, don't batch. One field per call is the safe default.

Note: if you batch, the retry layer becomes more complex (one bad item shouldn't invalidate the whole batch). Stick with single-field calls unless profiling shows latency is a real problem.

---

## Generator (LLM, free-text, ONE field at a time) — `app/services/generator.py`

### When it's called
Only for `textarea` fields where classifier returned `value=null` with reason "free-text question" (i.e. open-ended prompts like "Why do you want to work here?").

### Prompt shape

```
SYSTEM:
You are writing an answer to an open-ended job application question,
in first person, in the applicant's voice.

Rules:
- Be specific. Reference real projects/companies/achievements from the resume.
- Professional but human. Avoid corporate cliches.
- Respect max_length; aim comfortably under.
- Output just the answer text. No preamble, no quotes, no markdown.

USER:
JOB:
  title: {job.job_title}
  company: {job.company}
  description: {job.job_description[:3000]}

APPLICANT RESUME:
{profile.resume_text OR a compact JSON of profile without resume_text}

QUESTION:
{field.label}

Max length: {field.max_length or "aim 150-250 words"}
```

### Model: `GEMINI_MODEL_SMART` (default `gemini-2.5-pro`)
Pro-tier model chosen for quality — free-text answers go in front of recruiters.
If volume grows and cost becomes an issue, try `gemini-2.5-flash` and compare
output quality on real prompts before committing.

- `temperature: 0.7` — some creativity, not too wild
- `max_output_tokens: 1024`
- No `response_schema` needed — output is plain text, not JSON

### Parallelism
Multiple free-text fields are generated in parallel via `asyncio.gather`. Each is still a separate, focused call.

---

## Orchestrator — `app/services/filler.py`

Pulls it all together. Pseudocode:

```python
async def fill_form(schema, profile) -> (filled, unfilled):
    filled = []
    unfilled = []
    free_text_fields = []

    # PASS 1: rule-based matching (no LLM)
    llm_needed = []
    for field in schema.fields:
        result = matcher.match_field(field, profile)
        if result is None:
            llm_needed.append(field)
            continue
        if result.needs_option_match:
            # Rule narrowed intent but we need LLM to pick option text
            llm_needed.append(field)
            continue
        if result.value is None:
            unfilled.append(field)
            continue
        filled.append(FilledField(
            ..., value=result.value, confidence=result.confidence,
            reason=f"rule-match: {result.profile_key}",
            needs_review=result.confidence < 0.7,
        ))

    # PASS 2: LLM classifier, one field at a time (parallel)
    if llm_needed:
        results = await asyncio.gather(*[
            classifier.classify_field(f, profile) for f in llm_needed
        ])
        for field, result in zip(llm_needed, results):
            if result.value is None:
                if field.type == "textarea" and (field.max_length or 0) > 100:
                    free_text_fields.append(field)
                else:
                    unfilled.append(field)
                continue
            # Resolve radio option_selector from matched label
            option_selector = None
            if field.type == "radio_group":
                match = next((o for o in field.options
                             if o.label.lower() == result.value.lower()), None)
                if not match:
                    unfilled.append(field)
                    continue
                option_selector = match.selector
            filled.append(FilledField(
                ..., value=result.value, interaction=field.interaction,
                option_selector=option_selector,
                confidence=result.confidence, reason=result.reason,
                needs_review=result.confidence < 0.7,
            ))

    # PASS 3: free-text generation (parallel)
    if free_text_fields:
        answers = await asyncio.gather(*[
            generator.generate(f, profile, schema.job) for f in free_text_fields
        ], return_exceptions=True)
        for field, answer in zip(free_text_fields, answers):
            if isinstance(answer, Exception) or not answer:
                unfilled.append(field)
                continue
            filled.append(FilledField(
                ..., value=answer, interaction="type",
                confidence=0.85, reason="generated free-text",
                needs_review=True,
            ))

    return filled, unfilled
```

**Cost profile for a typical 20-field application:**
- Pass 1 (matcher): ~14 fields handled for free (rule-based, no LLM)
- Pass 2 (classifier): ~5 LLM calls on **Gemma 4 31B** — FREE on AI Studio free tier (14.4K RPD means ~2,800 applications/day at the free cap)
- Pass 3 (generator): ~1 LLM call on **Gemini 2.5 Pro** — ~$0.01–0.02 per application (quality matters here)
- **Total: ~$0.01–0.02 per application** — mostly from free-text generation
- **Free-tier ceiling:** roughly 2,800 applications/day from classifier quota before you start paying. More than enough for v1.

If you hit the Gemma free-tier cap, the spec's model-swap via env var lets you
move the classifier to paid Gemini Flash without touching code.

---

## Routes

### `POST /fill`
```python
from app.fixtures.dummy_profile import DUMMY_PROFILE

@router.post("/fill")
async def fill(req: FillRequest) -> FillResponse:
    url = str(req.url)
    if "myworkdayjobs.com" not in url and "myworkday.com" not in url:
        raise HTTPException(400, "URL doesn't look like a Workday job URL.")

    try:
        schema = await scraper.scrape(url)
    except Exception as e:
        raise HTTPException(502, f"Failed to scrape: {e}")

    if schema.status != "ok":
        # Map to 409 / 410 / 422 with diagnostics in detail

    filled, unfilled = await filler.fill_form(schema, DUMMY_PROFILE)
    return FillResponse(job=schema.job, filled=filled, unfilled=unfilled,
                       elapsed_ms=...)
```

**That's it — no profile endpoints in v1.** The dummy profile is imported at module level. When you're ready for Phase 2 (real profile storage), this is a 3-line change: add `user_id` to `FillRequest`, swap `DUMMY_PROFILE` for `store.get(req.user_id)`, return 404 if missing.

### `GET /health`
Always returns `{"status": "ok"}`. Used for liveness checks.

---

## Config (`app/config.py`)

Via `pydantic-settings`, loaded from `.env`:

```
# Single API key for AI Studio — works for both Gemma and Gemini models
GEMINI_API_KEY=<required>

# Classifier (called per field, many times — pick cheap model)
CLASSIFIER_MODEL=gemma-4-31b-it
# If Gemma's structured output proves unreliable at runtime, swap to:
# CLASSIFIER_MODEL=gemini-2.0-flash

# Generator (called for free-text only — pick quality model)
GENERATOR_MODEL=gemini-2.5-pro

# Retries for the classifier's JSON parsing layer
CLASSIFIER_MAX_RETRIES=1

# Playwright
HEADLESS=true
PAGE_TIMEOUT_MS=30000
```

Pydantic-settings class:
```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str
    classifier_model: str = "gemma-4-31b-it"
    generator_model: str = "gemini-2.5-pro"
    classifier_max_retries: int = 1

    headless: bool = True
    page_timeout_ms: int = 30000
```

---

## FastAPI Lifespan

`app/main.py` uses `lifespan` context to start/stop the Playwright browser once per process:
```python
@asynccontextmanager
async def lifespan(app):
    await scraper.start()
    yield
    await scraper.stop()
```

---

## Standalone Test Script — `scripts/test_scraper.py`

A CLI that runs just the scraper (no Gemini key needed) and prints detected status + fields grouped by type. Used to validate against real Workday URLs before trusting end-to-end.

```bash
python -m scripts.test_scraper "https://<company>.wd5.myworkdayjobs.com/.../job/..."
```

Prints:
- Scrape status
- Job title + company
- Step indicator
- Fields grouped by type, with label + interaction mode + option count
- Diagnostics dict

---

## Requirements

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.2
pydantic-settings==2.6.0
playwright==1.48.0
google-genai==0.3.0
python-dotenv==1.0.1
```

Post-install: `playwright install chromium`

---

## What This Backend Does NOT Do (v1 Scope Discipline)

- **No profile management.** Dummy profile only. No `POST /profile`, no persistence, no user accounts.
- **Submit forms.** Output is instructions for a client/extension to execute.
- **Parse resume PDFs.** `resume_text` is a plain string on the `UserProfile` model.
- **Authentication.** No users, no keys. Add in Phase 2.
- **Multi-step pagination.** Scraper detects step count but doesn't advance.
- **Upload files.** File-upload fields are detected (`type: "file"`) but the client handles the upload.
- **Handle login-gated Workday tenants.** Returns 409; surfacing auth to user is a client concern.

**Phase 2 adds:** profile CRUD, in-memory store (later Postgres), `user_id` in `FillRequest`. The `UserProfile` Pydantic model is already complete so Phase 2 is strictly additive.

---

## Validation Plan

Order of operations when building:

1. Build scraper + `scripts/test_scraper.py` first.
2. Run against 3–5 real public Workday URLs (NVIDIA, Salesforce, Adobe, etc.).
3. Inspect field counts and types. If anything looks off, check `/tmp/workday-dumps/`.
4. Only after scraper gives sane output, build matcher → classifier → generator.
5. **Before wiring classifier into the orchestrator**, sanity-check Gemma 4 31B's
   structured output reliability: run the classifier directly against 10–20
   sample fields and confirm the JSON parses cleanly on the first try in most
   cases (retry layer handles the rest). If it fails frequently (>20% first-try
   failures), flip `CLASSIFIER_MODEL=gemini-2.0-flash` in `.env` and move on.
6. End-to-end `/fill` test with a real profile and real URL last.

---

## Build Order (for Claude Code)

1. `app/models/schemas.py` — all Pydantic models (including `UserProfile` in full; `FillRequest` has NO `user_id` in v1)
2. `app/config.py` + `.env.example`
3. `app/fixtures/dummy_profile.py` — the hardcoded fake profile
4. `app/services/scraper.py` — Playwright + field extraction JS
5. `scripts/test_scraper.py` — validate scraper alone
6. `app/services/matcher.py` — rule-based matching
7. `app/services/classifier.py` — single-field LLM classifier
8. `app/services/generator.py` — free-text generator
9. `app/services/filler.py` — orchestrator (imports `DUMMY_PROFILE`)
10. `app/routers/fill.py` — the only real endpoint
11. `app/main.py`
12. `README.md`

---

# v1 — Account Creation & Sign-In

Most Workday tenants gate the application form behind a per-tenant Candidate Home account. v1 handles this flow end-to-end: create an account on the user's behalf (with their consent and real data), sign in on future visits, persist credentials per-tenant.

Without this, v1 can only serve the minority of tenants with public (un-gated) forms. With it, v1 works on the majority of Workday deployments.

**Key principle carried throughout:** we never invent user data. Every field we fill is either pulled from the user's real profile, generated deterministically (passwords), or surfaced to the user for input. The word "dummy" in this spec refers to the fake profile used in development — never to the identity used in account creation.

## User-consent model

Blanket consent at onboarding: the user agrees once that the tool may create accounts on their behalf at any Workday tenant when needed. Consent timestamp + version string are stored in the profile.

**Trade-off acknowledged:** per-tenant consent is more defensible if employer ToS get stricter. Blanket is chosen here for onboarding friction. If legal posture changes, this is the cleanest place to tighten — the scraper still shows the account-creation page before submitting, so upgrading to per-tenant approval is a UX change, not an architectural one.

## Email verification

Workday's default Candidate Home flow does NOT require email verification at account creation — sign-in works immediately after submit. However, a minority of tenants (often large enterprises with custom configs) DO require verification. The scraper handles both:

- **Common path:** create → sign in → continue to application
- **Verification-required path:** create → scraper detects verification-pending state → returns `status: "email_verification_required"` with instructions. User verifies in their inbox, returns, and retries. No automated inbox access on our end.

Password reset always requires email access — a known, accepted limitation.

## Credential storage

**Target design:**
- **Backend:** encrypted per-user, per-tenant credential records keyed by `(user_id, tenant)`. Encryption key derived from an env-level master key + user_id. Backend stores the ciphertext, never plaintext.
- **Client:** receives credentials in the `FillResponse` for every account-creation or account-refresh event. Stores locally (browser extension `storage`, mobile Keychain, etc.). Client is the authoritative source for the user; backend is a convenience copy for cross-device sync.

**Initial development (dummy credentials phase):**
- No real backend storage yet. A hardcoded `DUMMY_CREDENTIALS` dict in `app/fixtures/dummy_credentials.py` maps `tenant → {email, password}`. The scraper reads from this fixture instead of a real store. This mirrors how the rest of v1 uses `DUMMY_PROFILE`: prove the flow, then add real persistence.

The fixture is a temporary stand-in for what will become `services/credential_store.py` + a `POST /credentials` router in a later phase.

## New scrape statuses

```python
ScrapeStatus = Literal[
    "ok",
    "login_required",           # gate detected but no credentials provided — client should retry with creation flow
    "no_form_found",
    "job_closed",
    "unsupported_flow",
    # Account/auth outcomes:
    "account_created",          # successfully created + signed in, form now reachable
    "signed_in",                # used existing credentials, form reachable
    "email_verification_required",  # account made but tenant requires inbox click
    "invalid_credentials",      # stored creds failed — user needs to reset
    "account_creation_failed",  # captcha, rate limit, or field we couldn't fill
]
```

HTTP mapping for the new statuses:

| Status | HTTP | Meaning for the client |
|---|---|---|
| `account_created` / `signed_in` | 200 | Normal `FillResponse` (includes any new credentials) |
| `email_verification_required` | 202 | Account made; user must verify; include `tenant` + `email` in detail |
| `invalid_credentials` | 401 | Stored creds rejected; client should prompt user or clear the record |
| `account_creation_failed` | 422 | Give up, include `diagnostics` in detail |

## New data models

```python
class Credentials(BaseModel):
    tenant: str              # "nvidia.wd5.myworkdayjobs.com"
    email: str
    password: str            # plaintext in transit to client; encrypted at rest
    created_at: datetime
    verified: bool = True    # false if email_verification_required
    source: Literal["created", "provided"] = "created"  # we made it, or user gave it

class AccountAction(BaseModel):
    """What the scraper did about authentication on this request."""
    action: Literal["none", "signed_in", "created", "verification_pending"]
    tenant: str | None = None
    credentials: Credentials | None = None  # populated only when new or refreshed

class FillRequest(BaseModel):
    """Extended for account creation flow."""
    url: HttpUrl
    # client includes any credentials it has for the tenant
    known_credentials: Credentials | None = None

class FillResponse(BaseModel):
    """Extended for account creation flow."""
    job: JobContext
    filled: list[FilledField]
    unfilled: list[FormField]
    elapsed_ms: int
    # what happened on the auth side
    account_action: AccountAction
```

**Backward compatibility:** a v1 client that sends no `known_credentials` and ignores `account_action` still works. `account_action.action == "none"` means the tenant didn't require auth (most public application URLs today).

## Tenant extraction

```python
def extract_tenant(url: str) -> str | None:
    """nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/... → 'nvidia.wd5.myworkdayjobs.com'"""
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else None
```

The tenant string is the credential lookup key. All of Workday's public application URLs include the full host.

## Dummy credentials fixture — `app/fixtures/dummy_credentials.py`

```python
"""
Dummy per-tenant credentials for development.

Mirrors DUMMY_PROFILE: a hardcoded dict in place of a real credential store,
so the account-creation / sign-in flows can be validated before persistence
is built.

In production (later phase), replace this import in scraper.py with a
credential_store.get(user_id, tenant) lookup.
"""
from datetime import datetime, timezone
from app.models.schemas import Credentials


DUMMY_CREDENTIALS: dict[str, Credentials] = {
    # Pre-seed with one fake tenant so signed-in flow can be tested end-to-end
    # without requiring a real account-creation run first.
    "example-tenant.wd1.myworkdayjobs.com": Credentials(
        tenant="example-tenant.wd1.myworkdayjobs.com",
        email="alex.rivera.test@example.com",
        password="dev-only-placeholder-do-not-use-in-production",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        verified=True,
        source="created",
    ),
}


def get_dummy_credentials(tenant: str) -> Credentials | None:
    return DUMMY_CREDENTIALS.get(tenant)


def save_dummy_credentials(creds: Credentials) -> None:
    """In-memory write-through. Lost on restart. That's fine for dev."""
    DUMMY_CREDENTIALS[creds.tenant] = creds
```

## Password generation

Per-tenant random password. 20 chars, mixed case + digits + safe symbols. No shared secret across tenants.

```python
# app/services/password.py
import secrets
import string

_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"

def generate_password(length: int = 20) -> str:
    # Ensure at least one of each class to satisfy picky validators
    must_have = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*-_=+"),
    ]
    rest = [secrets.choice(_ALPHABET) for _ in range(length - len(must_have))]
    chars = must_have + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
```

## Scraper — new flows

Three new scraper states that extend the v1 state machine:

### State A: Sign-in flow (if `known_credentials` present)

1. Navigate to URL
2. Detect login wall (existing v1 logic)
3. If login wall present AND `known_credentials` present:
   - Find Sign-In form (pattern-match email + password inputs, "Sign In" button)
   - Type email, type password, submit
   - Wait for post-auth navigation
   - Check for `invalid_credentials` signals ("incorrect password", "account not found")
   - If signed in: `account_action = {action: "signed_in", tenant, credentials: None}` and continue to form extraction
   - If credentials rejected: return `status: "invalid_credentials"` immediately
4. If no login wall: `account_action = {action: "none"}`, continue as v1

### State B: Account creation (if login wall AND no `known_credentials`)

1. Find "Create Account" link (patterns: text match on "Create Account", "New User", `data-automation-id` containing `createAccount`)
2. Click through to the creation form
3. Extract form fields (reuse the v1 `_FIELD_EXTRACTION_JS`)
4. Generate password via `generate_password()`
5. Synthesize a `UserProfile`-like object that includes the generated password under a synthetic `password` key — the matcher already handles standard fields (name/email/phone), a new rule catches password/confirm-password fields
6. Run the full fill pipeline (matcher → classifier → generator) — same code path as v1
7. Submit the creation form
8. Check post-submit state:
   - **Verification-required signals:** `"check your email"`, `"verification link sent"`, `"confirm your email"` → return `status: "email_verification_required"`
   - **Creation-failed signals:** captcha challenge, rate-limit text, required-field errors → return `status: "account_creation_failed"` with diagnostics
   - **Success:** continue to application form. Build `Credentials` from the email used + generated password, set `account_action = {action: "created", tenant, credentials}`

### State C: Continue to application (after A or B succeeds)

- Same as v1 from here — extract form, return `FillResponse` with `account_action` filled in

### New matcher rules

Extend `RULES` in `matcher.py` with account-creation fields:

```python
ACCOUNT_RULES = [
  (r"^password$", "password", 0.98),
  (r"(confirm|verify|re-?enter)\s*password", "password", 0.95),  # same generated value
  (r"create\s*password", "password", 0.95),
  (r"security\s*question", None, 0.0),  # flag for LLM or user
]
```

Password and confirm-password both map to the same generated value.

## Orchestrator changes

`fill_form` gains a pre-pass: if `known_credentials` is passed and scraper returns `status="signed_in"`, no change. If scraper returns `status="account_created"`, the new credentials are included in the response's `account_action` so the client can store them.

```python
async def fill(req: FillRequest):
    tenant = extract_tenant(str(req.url))
    # dev-phase: read from fixture instead of real store
    creds = req.known_credentials or get_dummy_credentials(tenant)

    schema = await scraper.scrape(str(req.url), known_credentials=creds)

    # Handle all new statuses with proper HTTP codes
    if schema.status == "invalid_credentials":
        raise HTTPException(401, detail={...})
    if schema.status == "email_verification_required":
        return EmailVerificationResponse(...)  # HTTP 202
    if schema.status == "account_creation_failed":
        raise HTTPException(422, detail={...})
    # existing v1 handling for login_required / job_closed / etc.

    filled, unfilled = await fill_form(schema, DUMMY_PROFILE)

    # If scraper created a new account, remember it in the dummy store
    if schema.account_action and schema.account_action.action == "created":
        save_dummy_credentials(schema.account_action.credentials)

    return FillResponse(..., account_action=schema.account_action)
```

## Anti-abuse considerations

Creating accounts programmatically at volume will eventually trigger Workday's anti-fraud systems. For low-volume development and testing this isn't a concern. When scaling:

- Move Playwright execution to a browser extension (runs in user's session, looks like normal human traffic)
- If server-side is required, use residential proxies (Bright Data, Oxylabs)
- Rate-limit account creations per user per day
- Add explicit user confirmation UI before each account-creation (upgrades from blanket consent — see trade-off above)

None of this is in initial scope. It's later phase hardening.

## What the account-creation flow adds

1. `app/fixtures/dummy_credentials.py` — hardcoded credentials fixture (new file)
2. `app/services/password.py` — password generator (new file)
3. `app/services/scraper.py` — three new flow branches (extend existing file)
4. `app/services/matcher.py` — `ACCOUNT_RULES` additions (extend existing file)
5. `app/models/schemas.py` — `Credentials`, `AccountAction`, updated `FillRequest` / `FillResponse`, new `ScrapeStatus` values (extend existing file)
6. `app/routers/fill.py` — new status → HTTP code mapping, pass `known_credentials` through (extend existing file)

## What the account-creation flow does NOT do (deferred)

- Real credential persistence (backend encrypted store + `POST /credentials` endpoints)
- Per-tenant consent UI
- Password reset flow
- Email verification automation (user still handles the inbox click)
- Anti-abuse hardening (proxies, rate limits, extension migration)
- Re-creation if account deleted on tenant side

## Build order additions

1. Extend `schemas.py` with `Credentials`, `AccountAction`, new statuses — Phase A-style foundation update
2. `app/services/password.py` — trivial, no deps
3. `app/fixtures/dummy_credentials.py` — depends on schemas
4. `app/services/matcher.py` — add `ACCOUNT_RULES`
5. `app/services/scraper.py` — extend with three new flows, longest piece
6. `app/routers/fill.py` — wire new statuses + pass credentials through
7. End-to-end test: sign-in flow with dummy creds, account-creation flow on a real public Workday tenant that gates its form

## Validation plan additions

Test order, each before the next:

1. **Password generator** — unit-test that passwords pass common complexity requirements
2. **Sign-in flow against dummy creds on a controlled tenant** — use any free test Workday if available, or mock the login page in Playwright for CI
3. **Account creation against a real public Workday tenant that requires login** — pick one known to gate its form (large company careers site)
4. **Verification-required path** — find a tenant known to require email verification, confirm the `email_verification_required` status surfaces correctly without hanging
5. **Invalid-credentials path** — deliberately pass wrong password, confirm 401 returns cleanly