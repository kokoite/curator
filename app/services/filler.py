"""Orchestrator that coordinates matcher, classifier, and generator to fill a form.

Three-pass pipeline:
  Pass 1 — Rule-based matcher (sync, no I/O)
  Pass 2 — LLM classifier for unmatched fields (async, parallel)
  Pass 3 — LLM generator for free-text fields (async, parallel)
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.models.schemas import (
    FilledField,
    FormField,
    FormSchema,
    UserProfile,
)
from app.services.matcher import MatchResult, match_field
from app.services.classifier import ClassifyResult, classify_field
from app.services.generator import generate


def _resolve_option_selector(field: FormField, value: Any) -> str | None:
    """Find the option selector for a value that matches an option label."""
    if not field.options or value is None:
        return None
    str_value = str(value).strip().lower()
    for opt in field.options:
        if opt.label.strip().lower() == str_value:
            return opt.selector
    # Also try matching against boolean representations for yes/no
    if isinstance(value, bool):
        yes_labels = {"yes", "true", "y"}
        no_labels = {"no", "false", "n"}
        target = yes_labels if value else no_labels
        for opt in field.options:
            if opt.label.strip().lower() in target:
                return opt.selector
    return None


def _match_result_to_filled(
    field: FormField, result: MatchResult
) -> FilledField | None:
    """Convert a MatchResult (with needs_option_match=False) to a FilledField."""
    if result.value is None:
        return None
    return FilledField(
        field_id=field.field_id,
        selector=field.selector,
        label=field.label,
        value=result.value,
        interaction=field.interaction,
        option_selector=None,
        confidence=result.confidence,
        reason=f"rule-match:{result.profile_key}",
    )


def _match_with_option(
    field: FormField, result: MatchResult
) -> FilledField | None:
    """Resolve a needs_option_match MatchResult against field options."""
    if result.value is None:
        return None
    option_selector = _resolve_option_selector(field, result.value)
    if option_selector is None:
        # Try string representation of the value
        str_val = str(result.value)
        for opt in field.options:
            if opt.label.strip().lower() == str_val.strip().lower():
                option_selector = opt.selector
                return FilledField(
                    field_id=field.field_id,
                    selector=field.selector,
                    label=field.label,
                    value=opt.label,
                    interaction=field.interaction,
                    option_selector=option_selector,
                    confidence=result.confidence,
                    reason=f"rule-match:{result.profile_key}",
                )
        # No option matched — cannot fill
        return None
    # Determine the label to use as the value
    str_value = str(result.value).strip().lower()
    for opt in field.options:
        if opt.selector == option_selector:
            return FilledField(
                field_id=field.field_id,
                selector=field.selector,
                label=field.label,
                value=opt.label,
                interaction=field.interaction,
                option_selector=option_selector,
                confidence=result.confidence,
                reason=f"rule-match:{result.profile_key}",
            )
    return None


async def fill_form(
    schema: FormSchema,
    profile: UserProfile,
) -> tuple[list[FilledField], list[FormField]]:
    """Fill a scraped form using the three-pass pipeline.

    Returns (filled_fields, unfilled_fields).
    """
    filled: list[FilledField] = []
    unfilled: list[FormField] = []

    # Fields that need LLM classification (pass 2)
    llm_needed: list[FormField] = []
    # Fields that need free-text generation (pass 3)
    free_text_fields: list[FormField] = []

    # ── Pass 1: Rule-based matcher ──────────────────────────────────────
    for field in schema.fields:
        result = match_field(field, profile)

        if result is None:
            # No rule matched — escalate to LLM
            llm_needed.append(field)
            continue

        if result.needs_option_match:
            filled_field = _match_with_option(field, result)
            if filled_field is not None:
                filled.append(filled_field)
            else:
                # Option matching failed — escalate to LLM
                llm_needed.append(field)
            continue

        # Direct match
        filled_field = _match_result_to_filled(field, result)
        if filled_field is not None:
            filled.append(filled_field)
        else:
            llm_needed.append(field)

    # ── Pass 2: LLM classifier ─────────────────────────────────────────
    if llm_needed:
        classify_tasks = [
            classify_field(field, profile) for field in llm_needed
        ]
        classify_results: list[ClassifyResult] = await asyncio.gather(*classify_tasks)

        for field, result in zip(llm_needed, classify_results):
            # Free-text escape: textarea with value=None → generator
            if result.value is None and field.type == "textarea":
                free_text_fields.append(field)
                continue

            if result.value is None or result.confidence < 0.4:
                unfilled.append(field)
                continue

            # For radio_group / select: resolve option_selector
            option_selector = None
            if field.type in ("radio_group", "select", "combobox") and field.options:
                for opt in field.options:
                    if opt.label.strip().lower() == result.value.strip().lower():
                        option_selector = opt.selector
                        break

            filled.append(
                FilledField(
                    field_id=field.field_id,
                    selector=field.selector,
                    label=field.label,
                    value=result.value,
                    interaction=field.interaction,
                    option_selector=option_selector,
                    confidence=result.confidence,
                    reason=result.reason,
                )
            )

    # ── Pass 3: LLM generator for free-text fields ─────────────────────
    if free_text_fields:
        gen_tasks = [
            generate(field, profile, schema.job) for field in free_text_fields
        ]
        gen_results = await asyncio.gather(*gen_tasks, return_exceptions=True)

        for field, result in zip(free_text_fields, gen_results):
            if isinstance(result, BaseException) or not result:
                unfilled.append(field)
                continue

            filled.append(
                FilledField(
                    field_id=field.field_id,
                    selector=field.selector,
                    label=field.label,
                    value=result,
                    interaction=field.interaction,
                    option_selector=None,
                    confidence=0.85,
                    reason="llm-generated",
                )
            )

    return filled, unfilled
