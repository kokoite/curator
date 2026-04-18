"""All Pydantic v2 data models for the Workday Application Agent."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkExperience(BaseModel):
    model_config = ConfigDict(strict=False)

    company: str
    title: str
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None


class Education(BaseModel):
    model_config = ConfigDict(strict=False)

    institution: str
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class UserProfile(BaseModel):
    model_config = ConfigDict(strict=False)

    user_id: str
    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    address_line_1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    github_url: str | None = None
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    salary_expectation: int | None = None
    salary_currency: str = "USD"
    years_of_experience: int | None = None
    current_company: str | None = None
    current_title: str | None = None
    work_history: list[WorkExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    resume_text: str | None = None


FieldType = Literal[
    "text",
    "email",
    "phone",
    "number",
    "url",
    "textarea",
    "select",
    "combobox",
    "radio_group",
    "checkbox",
    "date",
    "file",
    "unknown",
]

InteractionMode = Literal[
    "type",
    "select_native",
    "combobox",
    "radio",
    "checkbox",
    "file",
    "date",
]


class FieldOption(BaseModel):
    model_config = ConfigDict(strict=False)

    label: str
    value: str | None = None
    selector: str | None = None


class FormField(BaseModel):
    model_config = ConfigDict(strict=False)

    field_id: str
    selector: str
    label: str
    type: FieldType
    interaction: InteractionMode
    required: bool
    max_length: int | None = None
    options: list[FieldOption] = Field(default_factory=list)
    placeholder: str | None = None
    automation_id: str | None = None


ScrapeStatus = Literal[
    "ok",
    "login_required",
    "no_form_found",
    "job_closed",
    "unsupported_flow",
]


class JobContext(BaseModel):
    model_config = ConfigDict(strict=False)

    url: str
    job_title: str | None = None
    company: str | None = None
    job_description: str | None = None


class FormSchema(BaseModel):
    model_config = ConfigDict(strict=False)

    status: ScrapeStatus
    job: JobContext
    fields: list[FormField] = Field(default_factory=list)
    current_step: int | None = None
    total_steps: int | None = None
    diagnostics: dict[str, str] = Field(default_factory=dict)


class FilledField(BaseModel):
    model_config = ConfigDict(strict=False)

    field_id: str
    selector: str
    label: str
    value: str | bool | list[str]
    interaction: InteractionMode
    option_selector: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str | None = None
    needs_review: bool = False


class FillRequest(BaseModel):
    model_config = ConfigDict(strict=False)

    url: str


class FillResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    job: JobContext
    filled: list[FilledField] = Field(default_factory=list)
    unfilled: list[FormField] = Field(default_factory=list)
    elapsed_ms: int
