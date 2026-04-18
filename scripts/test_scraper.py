"""Standalone CLI to test the Workday scraper against a real URL.

Usage:
    python -m scripts.test_scraper "https://<company>.wd5.myworkdayjobs.com/.../job/..."
    python -m scripts.test_scraper --help

No Gemini API key needed — this runs the scraper only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict

from app.models.schemas import FormSchema
from app.services.scraper import WorkdayScraper


def print_schema(schema: FormSchema, elapsed_ms: int) -> None:
    """Pretty-print a FormSchema grouped by field type."""
    print(f"\n{'=' * 60}")
    print(f"Scrape Status : {schema.status}")
    print(f"Job Title     : {schema.job.job_title or '(not found)'}")
    print(f"Company       : {schema.job.company or '(not found)'}")
    print(f"URL           : {schema.job.url}")
    if schema.job.job_description:
        desc = schema.job.job_description[:200]
        print(f"Description   : {desc}...")

    if schema.current_step is not None or schema.total_steps is not None:
        print(f"Step          : {schema.current_step} of {schema.total_steps}")

    print(f"Fields        : {len(schema.fields)}")
    print(f"Elapsed       : {elapsed_ms} ms")
    print(f"{'=' * 60}")

    if schema.fields:
        # Group by type
        by_type: dict[str, list] = defaultdict(list)
        for field in schema.fields:
            by_type[field.type].append(field)

        for ftype, fields in sorted(by_type.items()):
            print(f"\n--- {ftype.upper()} ({len(fields)} fields) ---")
            for f in fields:
                opts = f" [{len(f.options)} options]" if f.options else ""
                req = " *" if f.required else ""
                print(f"  {f.field_id}  {f.label!r}{req}  interaction={f.interaction}{opts}")
                if f.automation_id:
                    print(f"          aid={f.automation_id}")

    if schema.diagnostics:
        print(f"\n--- DIAGNOSTICS ---")
        for k, v in sorted(schema.diagnostics.items()):
            print(f"  {k}: {v}")

    print()


async def run(url: str, headless: bool) -> None:
    """Run the scraper against a single URL."""
    import os
    os.environ.setdefault("HEADLESS", str(headless).lower())
    # Ensure GEMINI_API_KEY is set (config requires it)
    os.environ.setdefault("GEMINI_API_KEY", "not-needed-for-scraper")

    scraper = WorkdayScraper()
    await scraper.start()

    try:
        start = time.perf_counter()
        schema, _account_action = await scraper.scrape(url)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        print_schema(schema, elapsed_ms)
    finally:
        await scraper.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the Workday scraper against a real URL (no Gemini key needed).",
        prog="python -m scripts.test_scraper",
    )
    parser.add_argument("url", nargs="?", help="Workday job application URL to scrape")
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible (non-headless) mode",
    )

    args = parser.parse_args()

    if not args.url:
        parser.print_help()
        sys.exit(0)

    asyncio.run(run(args.url, headless=not args.no_headless))


if __name__ == "__main__":
    main()
