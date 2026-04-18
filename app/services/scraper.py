"""Playwright-based scraper for Workday job application forms."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from app.models.schemas import (
    FieldOption,
    FormField,
    FormSchema,
    JobContext,
)

# ---------------------------------------------------------------------------
# Pattern-based automation-id detection (regex, never hardcoded exact IDs)
# ---------------------------------------------------------------------------
_AID_APPLY_BUTTON = r"(apply|adventure).*button"
_AID_APPLY_MANUALLY = r"applyManually|manualApplication"
_AID_JOB_HEADER = r"jobPostingHeader|jobTitle"
_AID_JOB_DESCRIPTION = r"jobPostingDescription|jobDescription"
_AID_STEP_INDICATOR = r"progressBar|stepIndicator|wizardStep"

# Login wall signals — need ≥2 to confirm
_LOGIN_SIGNALS = [
    "Create Account",
    "Sign In",
    "Already have an account",
    "Welcome back",
    "Returning User",
    "New User",
]

# Job closed signals
_CLOSED_SIGNALS = [
    "no longer accepting",
    "position has been filled",
    "job has been closed",
    "no longer available",
    "this position is closed",
    "job posting is no longer",
]

# Form readiness check (JS)
_FORM_READINESS_JS = """
() => {
  const hasNative = document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select'
  ).length > 0;
  const hasCombobox = document.querySelectorAll(
    '[role="combobox"], button[aria-haspopup="listbox"]'
  ).length > 0;
  return hasNative || hasCombobox;
}
"""

# ---------------------------------------------------------------------------
# Big JS extraction function — module-level constant per spec requirement
# ---------------------------------------------------------------------------
_FIELD_EXTRACTION_JS = """
() => {
  const fields = [];
  const seen = new Set();
  let fieldCounter = 0;

  function nextId() {
    fieldCounter++;
    return 'f_' + String(fieldCounter).padStart(4, '0');
  }

  // --- Label resolution (4-level fallback) ---
  function resolveLabel(el) {
    // 1. aria-label
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

    // 2. aria-labelledby
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\\s+/).map(id => {
        const ref = document.getElementById(id);
        return ref ? ref.textContent.trim() : '';
      }).filter(Boolean);
      if (parts.length > 0) return parts.join(' ');
    }

    // 3. <label for="id">
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label && label.textContent.trim()) return label.textContent.trim();
    }

    // 4. Walk up ancestor data-automation-id / data-fkit-id blocks
    let ancestor = el.parentElement;
    for (let i = 0; i < 8 && ancestor; i++) {
      const aid = ancestor.getAttribute('data-automation-id') || ancestor.getAttribute('data-fkit-id');
      if (aid) {
        const lbl = ancestor.querySelector('label, [role="label"], .gwt-Label');
        if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
      }
      ancestor = ancestor.parentElement;
    }

    // Fallback: name or placeholder
    return el.getAttribute('name') || el.getAttribute('placeholder') || '';
  }

  // --- Selector resolution (priority order) ---
  function resolveSelector(el) {
    // 1. data-automation-id
    const aid = el.getAttribute('data-automation-id');
    if (aid) return "[data-automation-id='" + aid + "']";

    // 2. id
    if (el.id) return '#' + CSS.escape(el.id);

    // 3. name
    const name = el.getAttribute('name');
    if (name) return "[name='" + CSS.escape(name) + "']";

    // Fallback: build a path-based selector
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const label = el.getAttribute('aria-label');
    if (label) return tag + "[aria-label='" + CSS.escape(label) + "']";
    if (type) return tag + "[type='" + type + "']";
    return tag;
  }

  function getAutomationId(el) {
    const aid = el.getAttribute('data-automation-id');
    if (aid) return aid;
    let ancestor = el.parentElement;
    for (let i = 0; i < 5 && ancestor; i++) {
      const a = ancestor.getAttribute('data-automation-id');
      if (a) return a;
      ancestor = ancestor.parentElement;
    }
    return null;
  }

  function dedupKey(selector, label) {
    return selector + '||' + label;
  }

  function addField(obj) {
    const key = dedupKey(obj.selector, obj.label);
    if (seen.has(key)) return;
    seen.add(key);
    fields.push(obj);
  }

  function mapInputType(typeAttr) {
    const t = (typeAttr || 'text').toLowerCase();
    const typeMap = {
      'text': ['text', 'type'],
      'email': ['email', 'type'],
      'tel': ['phone', 'type'],
      'number': ['number', 'type'],
      'url': ['url', 'type'],
      'date': ['date', 'date'],
      'file': ['file', 'file'],
      'checkbox': ['checkbox', 'checkbox'],
    };
    return typeMap[t] || ['text', 'type'];
  }

  // 1. Native inputs (excluding hidden/submit/button/radio)
  document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="radio"])').forEach(el => {
    const typeAttr = el.getAttribute('type') || 'text';
    if (typeAttr === 'checkbox') return; // handled separately
    const [fieldType, interaction] = mapInputType(typeAttr);
    const selector = resolveSelector(el);
    const label = resolveLabel(el);
    addField({
      field_id: nextId(),
      selector: selector,
      label: label,
      type: fieldType,
      interaction: interaction,
      required: el.required || el.getAttribute('aria-required') === 'true',
      max_length: el.maxLength > 0 && el.maxLength < 100000 ? el.maxLength : null,
      options: [],
      placeholder: el.getAttribute('placeholder') || null,
      automation_id: getAutomationId(el),
    });
  });

  // 2. Textareas
  document.querySelectorAll('textarea').forEach(el => {
    const selector = resolveSelector(el);
    const label = resolveLabel(el);
    addField({
      field_id: nextId(),
      selector: selector,
      label: label,
      type: 'textarea',
      interaction: 'type',
      required: el.required || el.getAttribute('aria-required') === 'true',
      max_length: el.maxLength > 0 && el.maxLength < 100000 ? el.maxLength : null,
      options: [],
      placeholder: el.getAttribute('placeholder') || null,
      automation_id: getAutomationId(el),
    });
  });

  // 3. Native <select>
  document.querySelectorAll('select').forEach(el => {
    const selector = resolveSelector(el);
    const label = resolveLabel(el);
    const options = Array.from(el.options).map(opt => ({
      label: opt.textContent.trim(),
      value: opt.value || null,
      selector: null,
    }));
    addField({
      field_id: nextId(),
      selector: selector,
      label: label,
      type: 'select',
      interaction: 'select_native',
      required: el.required || el.getAttribute('aria-required') === 'true',
      max_length: null,
      options: options,
      placeholder: null,
      automation_id: getAutomationId(el),
    });
  });

  // 4. ARIA comboboxes
  document.querySelectorAll('[role="combobox"], button[aria-haspopup="listbox"], [aria-haspopup="listbox"]').forEach(el => {
    if (el.tagName.toLowerCase() === 'select') return; // already handled
    const selector = resolveSelector(el);
    const label = resolveLabel(el);
    addField({
      field_id: nextId(),
      selector: selector,
      label: label,
      type: 'combobox',
      interaction: 'combobox',
      required: el.getAttribute('aria-required') === 'true',
      max_length: null,
      options: [],  // resolved at fill time
      placeholder: el.getAttribute('placeholder') || null,
      automation_id: getAutomationId(el),
    });
  });

  // 5. Radio groups (grouped by name)
  const radioGroups = {};
  document.querySelectorAll('input[type="radio"]').forEach(el => {
    const name = el.getAttribute('name') || ('_unnamed_radio_' + nextId());
    if (!radioGroups[name]) radioGroups[name] = [];
    radioGroups[name].push(el);
  });
  for (const [name, radios] of Object.entries(radioGroups)) {
    // Group label from <legend>, [role="radiogroup"], or ancestor data-automation-id
    let groupLabel = '';
    const firstRadio = radios[0];
    const radiogroup = firstRadio.closest('[role="radiogroup"]');
    if (radiogroup) {
      const legend = radiogroup.querySelector('legend');
      if (legend) groupLabel = legend.textContent.trim();
      if (!groupLabel) {
        const lbl = radiogroup.querySelector('label, [role="label"], .gwt-Label');
        if (lbl) groupLabel = lbl.textContent.trim();
      }
      if (!groupLabel) groupLabel = resolveLabel(radiogroup);
    }
    if (!groupLabel) {
      const fieldset = firstRadio.closest('fieldset');
      if (fieldset) {
        const legend = fieldset.querySelector('legend');
        if (legend) groupLabel = legend.textContent.trim();
      }
    }
    if (!groupLabel) {
      // Walk up to find ancestor with data-automation-id
      let anc = firstRadio.parentElement;
      for (let i = 0; i < 8 && anc; i++) {
        const aid = anc.getAttribute('data-automation-id');
        if (aid) {
          const lbl = anc.querySelector('label, [role="label"], .gwt-Label');
          if (lbl) { groupLabel = lbl.textContent.trim(); break; }
        }
        anc = anc.parentElement;
      }
    }
    if (!groupLabel) groupLabel = name;

    const options = radios.map(r => ({
      label: resolveLabel(r) || r.value || '',
      value: r.value || null,
      selector: resolveSelector(r),
    }));

    const groupSelector = radiogroup ? resolveSelector(radiogroup) : resolveSelector(firstRadio.closest('fieldset') || firstRadio);
    const isRequired = radios.some(r => r.required || r.getAttribute('aria-required') === 'true');

    addField({
      field_id: nextId(),
      selector: groupSelector,
      label: groupLabel,
      type: 'radio_group',
      interaction: 'radio',
      required: isRequired,
      max_length: null,
      options: options,
      placeholder: null,
      automation_id: getAutomationId(firstRadio),
    });
  }

  // 6. Checkboxes (individual, not grouped)
  document.querySelectorAll('input[type="checkbox"]').forEach(el => {
    const selector = resolveSelector(el);
    const label = resolveLabel(el);
    addField({
      field_id: nextId(),
      selector: selector,
      label: label,
      type: 'checkbox',
      interaction: 'checkbox',
      required: el.required || el.getAttribute('aria-required') === 'true',
      max_length: null,
      options: [],
      placeholder: null,
      automation_id: getAutomationId(el),
    });
  });

  return fields;
}
"""


# ---------------------------------------------------------------------------
# IST timezone for timestamps (per user preference)
# ---------------------------------------------------------------------------
_IST = timedelta(hours=5, minutes=30)


class WorkdayScraper:
    """Singleton Playwright scraper with start/stop lifecycle."""

    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        from app.config import settings

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=settings.headless)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def scrape(self, url: str) -> FormSchema:
        """Scrape a Workday job URL and return a FormSchema."""
        from app.config import settings

        diagnostics: dict[str, str] = {}
        job = JobContext(url=url)

        if not self._browser:
            raise RuntimeError("Scraper not started — call start() first")

        context: BrowserContext = await self._browser.new_context()
        page: Page = await context.new_page()
        page.set_default_timeout(settings.page_timeout_ms)

        try:
            # 1. Navigate
            await page.goto(url, wait_until="networkidle")

            # 2. Extract job context
            job = await self._extract_job_context(page, url, diagnostics)

            # 3. Detect terminal states
            status = await self._detect_status(page, diagnostics)
            if status == "job_closed":
                schema = FormSchema(
                    status="job_closed", job=job, diagnostics=diagnostics
                )
                await self._dump_diagnostics(page, schema, url)
                return schema

            if status == "login_required":
                schema = FormSchema(
                    status="login_required", job=job, diagnostics=diagnostics
                )
                await self._dump_diagnostics(page, schema, url)
                return schema

            # 4. Try to click Apply → Apply Manually if on job posting page
            await self._click_apply(page, diagnostics)

            # 5. Wait for form readiness
            try:
                await page.wait_for_function(
                    _FORM_READINESS_JS, timeout=settings.page_timeout_ms
                )
                diagnostics["form_readiness"] = "fields detected"
            except Exception:
                # Re-check terminal states after apply click
                status = await self._detect_status(page, diagnostics)
                if status != "ok":
                    schema = FormSchema(
                        status=status, job=job, diagnostics=diagnostics
                    )
                    await self._dump_diagnostics(page, schema, url)
                    return schema

                schema = FormSchema(
                    status="no_form_found", job=job, diagnostics=diagnostics
                )
                await self._dump_diagnostics(page, schema, url)
                return schema

            # 6. Extract fields via in-browser JS
            raw_fields = await page.evaluate(_FIELD_EXTRACTION_JS)
            fields = [FormField(**f) for f in raw_fields]
            diagnostics["field_count"] = str(len(fields))

            if not fields:
                schema = FormSchema(
                    status="no_form_found", job=job, diagnostics=diagnostics
                )
                await self._dump_diagnostics(page, schema, url)
                return schema

            # 7. Extract step indicator
            current_step, total_steps = await self._extract_step_indicator(
                page, diagnostics
            )

            return FormSchema(
                status="ok",
                job=job,
                fields=fields,
                current_step=current_step,
                total_steps=total_steps,
                diagnostics=diagnostics,
            )

        except Exception as e:
            diagnostics["error"] = str(e)[:500]
            schema = FormSchema(
                status="unsupported_flow", job=job, diagnostics=diagnostics
            )
            await self._dump_diagnostics(page, schema, url)
            return schema

        finally:
            await context.close()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _extract_job_context(
        self, page: Page, url: str, diagnostics: dict[str, str]
    ) -> JobContext:
        """Extract job title, company, and description from the page."""
        job_title = None
        company = None
        job_description = None

        try:
            # Try automation-id patterns for job header
            header_el = await self._find_by_aid_pattern(page, _AID_JOB_HEADER)
            if header_el:
                job_title = await header_el.inner_text()
                job_title = job_title.strip() if job_title else None
                diagnostics["job_title_strategy"] = "automation-id"

            # Try automation-id patterns for job description
            desc_el = await self._find_by_aid_pattern(page, _AID_JOB_DESCRIPTION)
            if desc_el:
                job_description = await desc_el.inner_text()
                job_description = job_description.strip()[:5000] if job_description else None
                diagnostics["job_desc_strategy"] = "automation-id"

            # Fallback: try common selectors / heading elements
            if not job_title:
                for sel in ["h1", "h2", "[class*='jobTitle']", "[class*='job-title']"]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            job_title = (await el.inner_text()).strip()
                            if job_title:
                                diagnostics["job_title_strategy"] = f"fallback:{sel}"
                                break
                    except Exception:
                        continue

            # Extract company from URL or page
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            # Workday URLs often have format: <company>.wd<N>.myworkdayjobs.com
            match = re.match(r"^([^.]+)\.wd\d+\.myworkdayjobs\.com", hostname)
            if match:
                company = match.group(1).replace("-", " ").title()
                diagnostics["company_strategy"] = "url-parse"

        except Exception as e:
            diagnostics["job_context_error"] = str(e)[:200]

        return JobContext(
            url=url,
            job_title=job_title,
            company=company,
            job_description=job_description,
        )

    async def _detect_status(
        self, page: Page, diagnostics: dict[str, str]
    ) -> str:
        """Detect login wall, job closed, or return 'ok'."""
        try:
            page_text = await page.inner_text("body")
            page_text_lower = page_text.lower()
        except Exception:
            return "ok"

        # Job closed detection
        closed_hits = sum(
            1 for signal in _CLOSED_SIGNALS if signal.lower() in page_text_lower
        )
        if closed_hits >= 1:
            diagnostics["job_closed_signals"] = str(closed_hits)
            return "job_closed"

        # Login wall detection — need ≥2 signals
        login_hits = sum(
            1 for signal in _LOGIN_SIGNALS if signal.lower() in page_text_lower
        )
        diagnostics["login_signal_count"] = str(login_hits)
        if login_hits >= 2:
            return "login_required"

        return "ok"

    async def _click_apply(
        self, page: Page, diagnostics: dict[str, str]
    ) -> None:
        """Try to click Apply → Apply Manually buttons if present."""
        # Strategy 1: automation-id regex
        apply_btn = await self._find_by_aid_pattern(page, _AID_APPLY_BUTTON)
        if apply_btn:
            try:
                await apply_btn.click()
                diagnostics["apply_strategy"] = "automation-id"
                await page.wait_for_load_state("networkidle")
            except Exception as e:
                diagnostics["apply_click_error"] = str(e)[:200]
        else:
            # Strategy 2: text-based fallback
            try:
                fallback = page.get_by_role("button", name=re.compile(r"apply", re.IGNORECASE))
                if await fallback.count() > 0:
                    await fallback.first.click()
                    diagnostics["apply_strategy"] = "text-fallback"
                    await page.wait_for_load_state("networkidle")
                else:
                    diagnostics["apply_strategy"] = "none-found"
            except Exception:
                diagnostics["apply_strategy"] = "none-found"

        # Try "Apply Manually" if present
        manual_btn = await self._find_by_aid_pattern(page, _AID_APPLY_MANUALLY)
        if manual_btn:
            try:
                await manual_btn.click()
                diagnostics["apply_manually_strategy"] = "automation-id"
                await page.wait_for_load_state("networkidle")
            except Exception as e:
                diagnostics["apply_manually_error"] = str(e)[:200]
        else:
            try:
                fallback = page.get_by_role(
                    "button", name=re.compile(r"apply\s*manually", re.IGNORECASE)
                )
                if await fallback.count() > 0:
                    await fallback.first.click()
                    diagnostics["apply_manually_strategy"] = "text-fallback"
                    await page.wait_for_load_state("networkidle")
            except Exception:
                pass

    async def _extract_step_indicator(
        self, page: Page, diagnostics: dict[str, str]
    ) -> tuple[int | None, int | None]:
        """Extract step indicator (current_step, total_steps)."""
        current_step = None
        total_steps = None

        try:
            # Try automation-id pattern
            step_el = await self._find_by_aid_pattern(page, _AID_STEP_INDICATOR)
            if step_el:
                text = await step_el.inner_text()
                # Try "Step X of Y" pattern
                m = re.search(r"(?:step\s+)?(\d+)\s*(?:of|/)\s*(\d+)", text, re.IGNORECASE)
                if m:
                    current_step = int(m.group(1))
                    total_steps = int(m.group(2))
                    diagnostics["step_strategy"] = "text-parse"

            # Fallback: count [role="listitem"] with aria-current
            if current_step is None:
                steps_js = """
                () => {
                    const items = document.querySelectorAll('[role="listitem"]');
                    if (items.length === 0) return null;
                    let current = 0;
                    items.forEach((item, i) => {
                        if (item.getAttribute('aria-current') === 'step') current = i + 1;
                    });
                    return current > 0 ? { current: current, total: items.length } : null;
                }
                """
                result = await page.evaluate(steps_js)
                if result:
                    current_step = result["current"]
                    total_steps = result["total"]
                    diagnostics["step_strategy"] = "listitem-count"

        except Exception as e:
            diagnostics["step_error"] = str(e)[:200]

        return current_step, total_steps

    async def _find_by_aid_pattern(self, page: Page, pattern: str):
        """Find first element whose data-automation-id matches the regex pattern."""
        try:
            js = f"""
            () => {{
                const pattern = new RegExp({repr(pattern)}, 'i');
                const all = document.querySelectorAll('[data-automation-id]');
                for (const el of all) {{
                    if (pattern.test(el.getAttribute('data-automation-id'))) return true;
                }}
                return false;
            }}
            """
            has_match = await page.evaluate(js)
            if not has_match:
                return None

            # Use locator to find the element
            elements = page.locator("[data-automation-id]")
            count = await elements.count()
            for i in range(count):
                el = elements.nth(i)
                aid = await el.get_attribute("data-automation-id")
                if aid and re.search(pattern, aid, re.IGNORECASE):
                    return el
        except Exception:
            pass
        return None

    async def _dump_diagnostics(
        self, page: Page, schema: FormSchema, url: str
    ) -> None:
        """Dump HTML + screenshot on non-ok status. Never throws."""
        if schema.status == "ok":
            return

        try:
            dump_dir = "/tmp/workday-dumps"
            os.makedirs(dump_dir, exist_ok=True)

            now = datetime.now(timezone(_IST))
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^a-zA-Z0-9]", "_", urlparse(url).path)[:60]
            base = f"{timestamp}_{schema.status}_{slug}"

            html_content = await page.content()
            html_path = os.path.join(dump_dir, f"{base}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            png_path = os.path.join(dump_dir, f"{base}.png")
            await page.screenshot(path=png_path, full_page=True)

            schema.diagnostics["dump_html"] = html_path
            schema.diagnostics["dump_screenshot"] = png_path

        except Exception as e:
            schema.diagnostics["dump_error"] = str(e)[:200]


# Module-level singleton
scraper = WorkdayScraper()
