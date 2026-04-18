"""Playwright-based scraper for Workday job application forms."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import logging

from app.models.schemas import (
    AccountAction,
    Credentials,
    FieldOption,
    FormField,
    FormSchema,
    JobContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern-based automation-id detection (regex, never hardcoded exact IDs)
# ---------------------------------------------------------------------------
_AID_APPLY_BUTTON = r"(apply|adventure).*button"
_AID_APPLY_MANUALLY = r"applyManually|manualApplication"
_AID_JOB_HEADER = r"jobPostingHeader|jobTitle"
_AID_JOB_DESCRIPTION = r"jobPostingDescription|jobDescription"
_AID_STEP_INDICATOR = r"progressBar|stepIndicator|wizardStep"
_AID_CREATE_ACCOUNT = r"createAccount|newUser|signUp|registerButton"

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

  // --- Visibility check (honeypot filter) ---
  function visible(el) {
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return false;
    if (cs.visibility === 'hidden') return false;
    if (parseFloat(cs.opacity) === 0) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
    if (el.offsetWidth <= 1 && el.offsetHeight <= 1 && cs.overflow !== 'visible') return false;
    return true;
  }

  // 1. Native inputs (excluding hidden/submit/button/radio)
  document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="radio"])').forEach(el => {
    const typeAttr = el.getAttribute('type') || 'text';
    if (typeAttr === 'checkbox') return; // handled separately
    if (!visible(el)) return;
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
    if (!visible(el)) return;
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
    if (!visible(el)) return;
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
    if (!visible(el)) return;
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
    // Filter out hidden individual radios
    const visibleRadios = radios.filter(r => visible(r));
    if (visibleRadios.length === 0) continue;

    // Group label from <legend>, [role="radiogroup"], or ancestor data-automation-id
    let groupLabel = '';
    const firstRadio = visibleRadios[0];
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

    const options = visibleRadios.map(r => ({
      label: resolveLabel(r) || r.value || '',
      value: r.value || null,
      selector: resolveSelector(r),
    }));

    const groupSelector = radiogroup ? resolveSelector(radiogroup) : resolveSelector(firstRadio.closest('fieldset') || firstRadio);
    const isRequired = visibleRadios.some(r => r.required || r.getAttribute('aria-required') === 'true');

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
    if (!visible(el)) return;
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


class _AccountProfile:
    """Thin wrapper that adds a ``password`` attribute to a UserProfile.

    The matcher uses ``getattr(profile, key)`` for value resolution.
    ACCOUNT_RULES map password/confirm-password fields to the key ``"password"``.
    This wrapper makes ``getattr(account_profile, "password")`` return the
    generated password, while all other attribute lookups delegate to the
    underlying real profile.
    """

    def __init__(self, profile, password: str) -> None:
        self._profile = profile
        self.password = password

    def __getattr__(self, name: str):
        return getattr(self._profile, name)


def extract_tenant(url: str) -> str | None:
    """Extract the tenant host from a URL.

    Returns the full host portion of the URL (e.g. "nvidia.wd5.myworkdayjobs.com").
    The tenant string is the credential lookup key.
    """
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else None


# Signals for invalid credentials after sign-in attempt
_INVALID_CRED_SIGNALS = [
    "incorrect",
    "invalid",
    "not found",
    "wrong password",
    "authentication failed",
    "does not match",
]

# Signals for email verification required after account creation
_VERIFICATION_SIGNALS = [
    "verify your email",
    "verification email",
    "check your email",
    "confirm your email",
    "email has been sent",
]


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

    async def scrape(
        self, url: str, known_credentials: Credentials | None = None
    ) -> tuple[FormSchema, AccountAction]:
        """Scrape a Workday job URL and return a FormSchema + AccountAction.

        When the page requires login:
          - If known_credentials are provided, attempt sign-in (Branch A).
          - Otherwise, attempt account creation (Branch B).
          - On auth success, continue to normal form extraction (Branch C).
        """
        from app.config import settings

        diagnostics: dict[str, str] = {}
        job = JobContext(url=url)
        account_action = AccountAction(action="none")

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
                return schema, account_action

            if status == "login_required":
                tenant = extract_tenant(url)
                logger.info("Login wall detected for tenant=%s", tenant)

                if known_credentials is not None:
                    # Branch A: Sign-in with provided credentials
                    logger.info("Attempting sign-in flow (Branch A)")
                    auth_status, account_action = await self._sign_in(
                        page, known_credentials, tenant, diagnostics
                    )
                else:
                    # Branch B: Account creation
                    logger.info("Attempting account-creation flow (Branch B)")
                    auth_status, account_action = await self._create_account(
                        page, tenant, diagnostics
                    )

                if auth_status != "ok":
                    schema = FormSchema(
                        status=auth_status, job=job, diagnostics=diagnostics
                    )
                    await self._dump_diagnostics(page, schema, url)
                    return schema, account_action

                # Branch C: Auth succeeded — continue to form extraction
                logger.info("Auth succeeded, continuing to form extraction (Branch C)")
                diagnostics["auth_flow"] = account_action.action

                # Post-creation sign-in: some tenants (e.g. NVIDIA) redirect
                # back to the sign-in page after account creation instead of
                # auto-logging in.  Detect this and sign in with the freshly
                # created credentials before proceeding.
                if account_action.action == "created":
                    post_status = await self._detect_status(page, diagnostics)
                    if post_status == "login_required":
                        logger.info("Post-creation sign-in required — tenant did not auto-login")
                        diagnostics["post_creation_signin"] = "attempting"
                        if account_action.credentials is None:
                            diagnostics["post_creation_signin"] = "no_credentials"
                            logger.error("Post-creation sign-in: no credentials available")
                            schema = FormSchema(
                                status="account_creation_failed", job=job, diagnostics=diagnostics
                            )
                            await self._dump_diagnostics(page, schema, url)
                            return schema, account_action
                        signin_status, _ = await self._sign_in(
                            page, account_action.credentials, tenant, diagnostics
                        )
                        if signin_status != "ok":
                            diagnostics["post_creation_signin"] = "failed"
                            logger.error("Post-creation sign-in failed with status=%s", signin_status)
                            await self._dump_auth_diagnostics(page, diagnostics, "post_create_signin_failed")
                            schema = FormSchema(
                                status="account_creation_failed", job=job, diagnostics=diagnostics
                            )
                            await self._dump_diagnostics(page, schema, url)
                            return schema, account_action
                        diagnostics["post_creation_signin"] = "success"
                        logger.info("Post-creation sign-in succeeded")

                # Re-extract job context after auth navigation
                job = await self._extract_job_context(page, url, diagnostics)

                # Try to click Apply after auth
                await self._click_apply(page, diagnostics)

            # 4. Try to click Apply → Apply Manually if on job posting page
            await self._click_apply(page, diagnostics)

            # 5. Wait for form readiness
            try:
                await page.wait_for_function(
                    _FORM_READINESS_JS, timeout=settings.page_timeout_ms
                )
                diagnostics["form_readiness"] = "fields detected"
            except Exception:
                # Re-check terminal states after apply click — the login
                # wall often only appears AFTER clicking Apply.
                status = await self._detect_status(page, diagnostics)

                if status == "login_required":
                    tenant = extract_tenant(url)
                    logger.info("Login wall detected post-apply for tenant=%s", tenant)

                    if known_credentials is not None:
                        logger.info("Attempting sign-in flow (Branch A, post-apply)")
                        auth_status, account_action = await self._sign_in(
                            page, known_credentials, tenant, diagnostics
                        )
                    else:
                        logger.info("Attempting account-creation flow (Branch B, post-apply)")
                        auth_status, account_action = await self._create_account(
                            page, tenant, diagnostics
                        )

                    if auth_status != "ok":
                        schema = FormSchema(
                            status=auth_status, job=job, diagnostics=diagnostics
                        )
                        await self._dump_diagnostics(page, schema, url)
                        return schema, account_action

                    # Auth succeeded — continue to form extraction
                    logger.info("Auth succeeded post-apply, continuing (Branch C)")
                    diagnostics["auth_flow"] = account_action.action

                    # Post-creation sign-in (same check as pre-apply path)
                    if account_action.action == "created":
                        post_status = await self._detect_status(page, diagnostics)
                        if post_status == "login_required":
                            logger.info("Post-creation sign-in required (post-apply)")
                            diagnostics["post_creation_signin"] = "attempting"
                            if account_action.credentials is None:
                                diagnostics["post_creation_signin"] = "no_credentials"
                                logger.error("Post-creation sign-in: no credentials available")
                                schema = FormSchema(
                                    status="account_creation_failed", job=job, diagnostics=diagnostics
                                )
                                await self._dump_diagnostics(page, schema, url)
                                return schema, account_action
                            signin_status, _ = await self._sign_in(
                                page, account_action.credentials, tenant, diagnostics
                            )
                            if signin_status != "ok":
                                diagnostics["post_creation_signin"] = "failed"
                                logger.error("Post-creation sign-in failed (post-apply)")
                                await self._dump_auth_diagnostics(page, diagnostics, "post_create_signin_failed")
                                schema = FormSchema(
                                    status="account_creation_failed", job=job, diagnostics=diagnostics
                                )
                                await self._dump_diagnostics(page, schema, url)
                                return schema, account_action
                            diagnostics["post_creation_signin"] = "success"
                            logger.info("Post-creation sign-in succeeded (post-apply)")

                    job = await self._extract_job_context(page, url, diagnostics)

                elif status != "ok":
                    schema = FormSchema(
                        status=status, job=job, diagnostics=diagnostics
                    )
                    await self._dump_diagnostics(page, schema, url)
                    return schema, account_action
                else:
                    schema = FormSchema(
                        status="no_form_found", job=job, diagnostics=diagnostics
                    )
                    await self._dump_diagnostics(page, schema, url)
                    return schema, account_action

            # 6. Extract fields via in-browser JS
            raw_fields = await page.evaluate(_FIELD_EXTRACTION_JS)
            fields = [FormField(**f) for f in raw_fields]
            diagnostics["field_count"] = str(len(fields))

            if not fields:
                schema = FormSchema(
                    status="no_form_found", job=job, diagnostics=diagnostics
                )
                await self._dump_diagnostics(page, schema, url)
                return schema, account_action

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
            ), account_action

        except Exception as e:
            diagnostics["error"] = str(e)[:500]
            schema = FormSchema(
                status="unsupported_flow", job=job, diagnostics=diagnostics
            )
            await self._dump_diagnostics(page, schema, url)
            return schema, account_action

        finally:
            await context.close()

    # -----------------------------------------------------------------------
    # Auth flow helpers (Branches A, B, C)
    # -----------------------------------------------------------------------

    async def _sign_in(
        self,
        page: Page,
        credentials: Credentials,
        tenant: str | None,
        diagnostics: dict[str, str],
    ) -> tuple[str, AccountAction]:
        """Branch A: Sign in with known credentials.

        Returns (status, AccountAction) where status is "ok" on success or an
        error ScrapeStatus literal on failure.
        """
        logger.info("Sign-in flow: looking for sign-in form")
        diagnostics["auth_branch"] = "sign_in"

        try:
            # Look for a "Sign In" button/link to navigate to the sign-in form
            sign_in_btn = page.get_by_role(
                "button", name=re.compile(r"sign\s*in", re.IGNORECASE)
            )
            if await sign_in_btn.count() > 0:
                await sign_in_btn.first.click()
                await page.wait_for_load_state("networkidle")
                logger.info("Sign-in flow: clicked Sign In button")
            else:
                # Try link
                sign_in_link = page.get_by_role(
                    "link", name=re.compile(r"sign\s*in", re.IGNORECASE)
                )
                if await sign_in_link.count() > 0:
                    await sign_in_link.first.click()
                    await page.wait_for_load_state("networkidle")
                    logger.info("Sign-in flow: clicked Sign In link")

            # Fill email field
            email_input = page.locator(
                'input[type="email"], input[name*="email" i], '
                'input[autocomplete="email"], input[data-automation-id*="email" i]'
            )
            if await email_input.count() > 0:
                await email_input.first.fill(credentials.email)
                logger.info("Sign-in flow: filled email")
            else:
                diagnostics["sign_in_error"] = "email input not found"
                logger.warning("Sign-in flow: email input not found")
                await self._dump_auth_diagnostics(page, diagnostics, "sign_in_no_email")
                return "invalid_credentials", AccountAction(action="none")

            # Fill password field
            password_input = page.locator('input[type="password"]')
            if await password_input.count() > 0:
                await password_input.first.fill(credentials.password)
                logger.info("Sign-in flow: filled password")
            else:
                diagnostics["sign_in_error"] = "password input not found"
                logger.warning("Sign-in flow: password input not found")
                await self._dump_auth_diagnostics(page, diagnostics, "sign_in_no_password")
                return "invalid_credentials", AccountAction(action="none")

            # Click submit (force=True: Workday click_filter overlay)
            submit_btn = page.locator(
                'button[type="submit"], '
                'button:has-text("Sign In"), button:has-text("Log In")'
            )
            if await submit_btn.count() > 0:
                await submit_btn.first.click(force=True)
            else:
                # Try pressing Enter as fallback
                await password_input.first.press("Enter")

            await page.wait_for_load_state("networkidle")
            logger.info("Sign-in flow: submitted credentials")

            # Check for invalid credential signals
            try:
                page_text = (await page.inner_text("body")).lower()
            except Exception:
                page_text = ""

            for signal in _INVALID_CRED_SIGNALS:
                if signal in page_text:
                    diagnostics["sign_in_error"] = f"invalid_creds_signal: {signal}"
                    logger.warning("Sign-in flow: invalid credentials detected (%s)", signal)
                    await self._dump_auth_diagnostics(page, diagnostics, "sign_in_invalid")
                    return "invalid_credentials", AccountAction(action="none")

            # Success
            logger.info("Sign-in flow: signed in successfully")
            diagnostics["sign_in_result"] = "success"
            return "ok", AccountAction(
                action="signed_in",
                tenant=tenant,
                credentials=None,
            )

        except Exception as e:
            diagnostics["sign_in_error"] = str(e)[:500]
            logger.error("Sign-in flow failed: %s", e)
            await self._dump_auth_diagnostics(page, diagnostics, "sign_in_error")
            return "invalid_credentials", AccountAction(action="none")

    async def _create_account(
        self,
        page: Page,
        tenant: str | None,
        diagnostics: dict[str, str],
    ) -> tuple[str, AccountAction]:
        """Branch B: Create a new Workday Candidate Home account.

        1. Find and click "Create Account" / "New User" (automation-id + text).
        2. Extract the creation form fields via _FIELD_EXTRACTION_JS.
        3. Synthesize a profile with a ``password`` attribute.
        4. Run the full fill pipeline (matcher → classifier → generator).
        5. Apply fill results to the page and submit.
        6. Inspect post-submit state.

        Returns (status, AccountAction) where status is "ok" on success or an
        error ScrapeStatus literal on failure.
        """
        from app.fixtures.dummy_profile import DUMMY_PROFILE
        from app.services.filler import fill_form
        from app.services.password import generate_password

        logger.info("Account-creation flow: looking for create-account trigger")
        diagnostics["auth_branch"] = "create_account"

        try:
            # ── Step 1: Click through to the creation form ──────────────
            clicked = await self._click_create_account(page, diagnostics)
            if not clicked:
                diagnostics["create_account_error"] = "no create-account trigger found"
                logger.warning("Account-creation flow: no create-account trigger found")
                await self._dump_auth_diagnostics(page, diagnostics, "create_no_button")
                return "account_creation_failed", AccountAction(action="none")

            # ── Step 2: Extract creation form fields ────────────────────
            from app.config import settings

            try:
                await page.wait_for_function(
                    _FORM_READINESS_JS, timeout=settings.page_timeout_ms
                )
            except Exception:
                diagnostics["create_account_error"] = "creation form not ready"
                logger.warning("Account-creation flow: form fields never appeared")
                await self._dump_auth_diagnostics(page, diagnostics, "create_no_form")
                return "account_creation_failed", AccountAction(action="none")

            raw_fields = await page.evaluate(_FIELD_EXTRACTION_JS)
            fields = [FormField(**f) for f in raw_fields]
            diagnostics["create_form_field_count"] = str(len(fields))
            logger.info("Account-creation flow: extracted %d form fields", len(fields))

            if not fields:
                diagnostics["create_account_error"] = "no fields in creation form"
                await self._dump_auth_diagnostics(page, diagnostics, "create_empty_form")
                return "account_creation_failed", AccountAction(action="none")

            # ── Step 3: Generate password and build synthetic profile ───
            password = generate_password()

            # _AccountProfile delegates attribute lookups to DUMMY_PROFILE
            # and adds a synthetic "password" attribute so the matcher's
            # ACCOUNT_RULES can resolve password fields via getattr.
            account_profile = _AccountProfile(DUMMY_PROFILE, password)

            # Build a FormSchema so fill_form can consume it
            create_schema = FormSchema(
                status="ok",
                job=JobContext(url=""),
                fields=fields,
                diagnostics={},
            )

            # ── Step 4: Run the full fill pipeline ─────────────────────
            filled, unfilled = await fill_form(create_schema, account_profile)
            diagnostics["create_form_filled_count"] = str(len(filled))
            diagnostics["create_form_unfilled_count"] = str(len(unfilled))
            logger.info(
                "Account-creation flow: fill pipeline returned %d filled, %d unfilled",
                len(filled), len(unfilled),
            )

            # ── Step 5: Apply fill results to the page ─────────────────
            applied = await self._apply_fill_results(page, filled, diagnostics)
            diagnostics["create_form_applied_count"] = str(applied)
            logger.info("Account-creation flow: applied %d fields to page", applied)

            # ── Step 6: Submit the form ────────────────────────────────
            # Workday wraps buttons with click_filter overlay divs that
            # intercept pointer events — use force=True to click through.
            submit_btn = page.locator(
                'button[type="submit"], '
                'button:has-text("Create Account"), button:has-text("Sign Up"), '
                'button:has-text("Register")'
            )
            if await submit_btn.count() > 0:
                await submit_btn.first.click(force=True)
            else:
                pw_inputs = page.locator('input[type="password"]')
                if await pw_inputs.count() > 0:
                    await pw_inputs.last.press("Enter")

            await page.wait_for_load_state("networkidle")
            logger.info("Account-creation flow: submitted account form")

            # ── Step 7: Inspect post-submit state ──────────────────────
            try:
                page_text = (await page.inner_text("body")).lower()
            except Exception:
                page_text = ""

            # 7a. Email verification required
            for signal in _VERIFICATION_SIGNALS:
                if signal in page_text:
                    diagnostics["create_account_result"] = "email_verification_required"
                    logger.info("Account-creation flow: email verification required")

                    now = datetime.now(timezone(_IST))
                    creds = Credentials(
                        tenant=tenant or "",
                        email=DUMMY_PROFILE.email,
                        password=password,
                        created_at=now,
                        verified=False,
                        source="created",
                    )
                    from app.fixtures.dummy_credentials import save_dummy_credentials
                    save_dummy_credentials(creds)

                    return "email_verification_required", AccountAction(
                        action="verification_pending",
                        tenant=tenant,
                        credentials=creds,
                    )

            # 7b. Captcha / rate-limit / required-field / other failures
            _FAILURE_SIGNALS = [
                "already exists",
                "already registered",
                "account already",
                "error creating",
                "captcha",
                "rate limit",
                "too many requests",
                "required field",
                "this field is required",
                "please complete",
            ]
            for signal in _FAILURE_SIGNALS:
                if signal in page_text:
                    diagnostics["create_account_error"] = f"failure_signal: {signal}"
                    logger.warning("Account-creation flow: failed (%s)", signal)
                    await self._dump_auth_diagnostics(page, diagnostics, "create_failed")
                    return "account_creation_failed", AccountAction(action="none")

            # 7c. Success — account created and logged in
            logger.info("Account-creation flow: account created successfully")
            diagnostics["create_account_result"] = "success"

            now = datetime.now(timezone(_IST))
            creds = Credentials(
                tenant=tenant or "",
                email=DUMMY_PROFILE.email,
                password=password,
                created_at=now,
                verified=True,
                source="created",
            )
            from app.fixtures.dummy_credentials import save_dummy_credentials
            save_dummy_credentials(creds)

            return "ok", AccountAction(
                action="created",
                tenant=tenant,
                credentials=creds,
            )

        except Exception as e:
            diagnostics["create_account_error"] = str(e)[:500]
            logger.error("Account-creation flow failed: %s", e)
            await self._dump_auth_diagnostics(page, diagnostics, "create_error")
            return "account_creation_failed", AccountAction(action="none")

    async def _click_create_account(
        self, page: Page, diagnostics: dict[str, str]
    ) -> bool:
        """Find and click the Create Account / New User trigger.

        Tries automation-id pattern first, then text-based matching on buttons
        and links. Returns True if a trigger was found and clicked.
        """
        # Strategy 1: automation-id regex
        aid_el = await self._find_by_aid_pattern(page, _AID_CREATE_ACCOUNT)
        if aid_el:
            try:
                await aid_el.click()
                diagnostics["create_account_trigger"] = "automation-id"
                await page.wait_for_load_state("networkidle")
                logger.info("Account-creation flow: clicked trigger via automation-id")
                return True
            except Exception as e:
                diagnostics["create_account_aid_error"] = str(e)[:200]

        # Strategy 2: text-based button
        _CREATE_TEXT_RE = re.compile(
            r"create\s*account|new\s*user|sign\s*up", re.IGNORECASE
        )
        create_btn = page.get_by_role("button", name=_CREATE_TEXT_RE)
        if await create_btn.count() > 0:
            await create_btn.first.click()
            diagnostics["create_account_trigger"] = "button-text"
            await page.wait_for_load_state("networkidle")
            logger.info("Account-creation flow: clicked Create Account button")
            return True

        # Strategy 3: text-based link
        create_link = page.get_by_role("link", name=_CREATE_TEXT_RE)
        if await create_link.count() > 0:
            await create_link.first.click()
            diagnostics["create_account_trigger"] = "link-text"
            await page.wait_for_load_state("networkidle")
            logger.info("Account-creation flow: clicked Create Account link")
            return True

        # Strategy 4: Workday email-first gateway — many tenants show
        # "Sign in with email" first; the Create Account option only appears
        # after entering an unrecognized email address.  Click the email
        # sign-in button, enter the dummy email, submit, then re-check for
        # a Create Account trigger.
        email_btn = await self._find_by_aid_pattern(page, r"SignInWithEmailButton")
        if not email_btn:
            email_btn_loc = page.get_by_role(
                "button", name=re.compile(r"sign\s*in\s*with\s*email", re.IGNORECASE)
            )
            if await email_btn_loc.count() > 0:
                email_btn = email_btn_loc.first

        if email_btn:
            try:
                await email_btn.click()
                await page.wait_for_load_state("networkidle")
                logger.info("Account-creation flow: clicked 'Sign in with email' gateway")
                diagnostics["create_account_gateway"] = "sign-in-with-email"

                # Enter the dummy email to discover the Create Account flow
                from app.fixtures.dummy_profile import DUMMY_PROFILE

                email_input = page.locator(
                    'input[type="email"], input[type="text"][data-automation-id*="email" i], '
                    'input[data-automation-id*="email" i]'
                )
                if await email_input.count() > 0:
                    await email_input.first.fill(DUMMY_PROFILE.email)
                    logger.info("Account-creation flow: entered email in gateway")

                    # Submit the email (look for Continue / Submit / Sign In)
                    submit = page.locator(
                        'button[type="submit"], '
                        'button[data-automation-id*="submit" i], '
                        'button[data-automation-id*="signIn" i]'
                    )
                    if await submit.count() > 0:
                        await submit.first.click(force=True)
                    else:
                        await email_input.first.press("Enter")

                    await page.wait_for_load_state("networkidle")
                    logger.info("Account-creation flow: submitted email in gateway")

                    # Now re-check for Create Account trigger.
                    # Use force=True because Workday's sign-in modal overlay
                    # can intercept pointer events on the Create Account button.
                    aid_el = await self._find_by_aid_pattern(page, _AID_CREATE_ACCOUNT)
                    if aid_el:
                        await aid_el.click(force=True)
                        diagnostics["create_account_trigger"] = "email-gateway-then-aid"
                        await page.wait_for_load_state("networkidle")
                        return True

                    create_btn = page.get_by_role("button", name=_CREATE_TEXT_RE)
                    if await create_btn.count() > 0:
                        await create_btn.first.click(force=True)
                        diagnostics["create_account_trigger"] = "email-gateway-then-button"
                        await page.wait_for_load_state("networkidle")
                        return True

                    create_link = page.get_by_role("link", name=_CREATE_TEXT_RE)
                    if await create_link.count() > 0:
                        await create_link.first.click(force=True)
                        diagnostics["create_account_trigger"] = "email-gateway-then-link"
                        await page.wait_for_load_state("networkidle")
                        return True

                    # The gateway may have landed directly on a creation form
                    # (some tenants skip to account creation for unknown emails)
                    pw_inputs = page.locator('input[type="password"]')
                    if await pw_inputs.count() > 0:
                        diagnostics["create_account_trigger"] = "email-gateway-direct-form"
                        logger.info("Account-creation flow: gateway led directly to creation form")
                        return True

            except Exception as e:
                diagnostics["create_account_gateway_error"] = str(e)[:200]
                logger.warning("Account-creation flow: email gateway failed: %s", e)

        return False

    async def _apply_fill_results(
        self,
        page: Page,
        filled: list,
        diagnostics: dict[str, str],
    ) -> int:
        """Apply FilledField results to the live page via Playwright.

        Returns the number of fields successfully applied.
        """
        applied = 0
        for ff in filled:
            try:
                if ff.interaction == "type":
                    await page.fill(ff.selector, str(ff.value))
                elif ff.interaction == "select_native":
                    await page.select_option(ff.selector, label=str(ff.value))
                elif ff.interaction == "combobox":
                    await page.click(ff.selector, force=True)
                    await page.fill(ff.selector, str(ff.value))
                    await page.wait_for_timeout(500)
                    await page.keyboard.press("Enter")
                elif ff.interaction == "radio" and ff.option_selector:
                    await page.click(ff.option_selector, force=True)
                elif ff.interaction == "checkbox":
                    is_checked = await page.is_checked(ff.selector)
                    should_check = ff.value is True or str(ff.value).lower() in (
                        "true", "yes", "1"
                    )
                    if should_check != is_checked:
                        await page.click(ff.selector, force=True)
                else:
                    # Fallback: try fill
                    await page.fill(ff.selector, str(ff.value))

                applied += 1
                logger.debug("Applied field %s = %r", ff.label, ff.value)

            except Exception as e:
                logger.warning(
                    "Account-creation flow: failed to apply field %s (%s): %s",
                    ff.label, ff.selector, e,
                )

        return applied

    async def _dump_auth_diagnostics(
        self, page: Page, diagnostics: dict[str, str], label: str
    ) -> None:
        """Save HTML + screenshot for auth flow failures. Never throws."""
        try:
            dump_dir = "/tmp/workday-dumps"
            os.makedirs(dump_dir, exist_ok=True)

            now = datetime.now(timezone(_IST))
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            base = f"{timestamp}_auth_{label}"

            html_content = await page.content()
            html_path = os.path.join(dump_dir, f"{base}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            png_path = os.path.join(dump_dir, f"{base}.png")
            await page.screenshot(path=png_path, full_page=True)

            diagnostics[f"auth_dump_html_{label}"] = html_path
            diagnostics[f"auth_dump_screenshot_{label}"] = png_path
            logger.info("Auth diagnostics dumped: %s", base)

        except Exception as e:
            diagnostics[f"auth_dump_error_{label}"] = str(e)[:200]

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
