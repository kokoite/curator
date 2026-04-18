"""Hardcoded dummy user profile for testing and development."""

from app.models.schemas import Education, UserProfile, WorkExperience

DUMMY_PROFILE = UserProfile(
    user_id="dummy-001",
    first_name="Priya",
    last_name="Sharma",
    email="priya.sharma@example.com",
    phone="+1-555-867-5309",
    address_line_1="742 Evergreen Terrace",
    city="San Francisco",
    state="CA",
    postal_code="94110",
    country="United States",
    linkedin_url="https://linkedin.com/in/priyasharma",
    portfolio_url="https://priyasharma.dev",
    github_url="https://github.com/priyasharma",
    work_authorized=True,
    requires_sponsorship=False,
    salary_expectation=165000,
    salary_currency="USD",
    years_of_experience=7,
    current_company="Acme Corp",
    current_title="Senior Software Engineer",
    work_history=[
        WorkExperience(
            company="Acme Corp",
            title="Senior Software Engineer",
            start_date="2021-03",
            end_date=None,
            description=(
                "Led backend architecture for the real-time analytics platform serving "
                "50M events/day. Designed and shipped a Kafka-based ingestion pipeline "
                "that reduced processing latency from 12s to 800ms. Mentored a team of "
                "4 engineers and drove adoption of structured logging across 30+ services."
            ),
        ),
        WorkExperience(
            company="Initech",
            title="Software Engineer",
            start_date="2018-06",
            end_date="2021-02",
            description=(
                "Built and maintained REST APIs powering the core billing system "
                "processing $2M+ in daily transactions. Migrated legacy monolith to "
                "microservices on Kubernetes, cutting deploy times from 45min to 3min. "
                "Implemented CI/CD pipelines and improved test coverage from 40% to 88%."
            ),
        ),
        WorkExperience(
            company="StartupXYZ",
            title="Junior Developer",
            start_date="2016-09",
            end_date="2018-05",
            description=(
                "Full-stack development on a B2B SaaS platform using Python/Django and "
                "React. Owned the notification subsystem end-to-end, supporting email, "
                "SMS, and in-app channels for 15K active users."
            ),
        ),
    ],
    education=[
        Education(
            institution="University of California, Berkeley",
            degree="B.S.",
            field_of_study="Computer Science",
            start_date="2012-08",
            end_date="2016-05",
        ),
    ],
    skills=[
        "Python",
        "FastAPI",
        "Django",
        "PostgreSQL",
        "Kafka",
        "Kubernetes",
        "Docker",
        "AWS",
        "TypeScript",
        "React",
        "CI/CD",
        "System Design",
    ],
    resume_text=(
        "Priya Sharma — Senior Software Engineer\n"
        "San Francisco, CA | priya.sharma@example.com | +1-555-867-5309\n"
        "LinkedIn: linkedin.com/in/priyasharma | GitHub: github.com/priyasharma\n\n"
        "SUMMARY\n"
        "Backend-focused engineer with 7 years of experience building scalable "
        "distributed systems. Passionate about clean architecture, observability, "
        "and shipping reliable software at scale.\n\n"
        "EXPERIENCE\n"
        "Senior Software Engineer — Acme Corp (Mar 2021 – Present)\n"
        "• Led backend architecture for real-time analytics platform serving 50M events/day\n"
        "• Designed Kafka-based ingestion pipeline reducing latency from 12s to 800ms\n"
        "• Mentored team of 4 engineers; drove structured logging across 30+ services\n"
        "• Technologies: Python, FastAPI, Kafka, PostgreSQL, Kubernetes, AWS\n\n"
        "Software Engineer — Initech (Jun 2018 – Feb 2021)\n"
        "• Built REST APIs for core billing system processing $2M+/day in transactions\n"
        "• Migrated monolith to microservices on K8s, cutting deploy times 45min → 3min\n"
        "• Implemented CI/CD pipelines; improved test coverage 40% → 88%\n"
        "• Technologies: Python, Django, PostgreSQL, Docker, Jenkins\n\n"
        "Junior Developer — StartupXYZ (Sep 2016 – May 2018)\n"
        "• Full-stack B2B SaaS development with Python/Django and React\n"
        "• Owned notification subsystem (email, SMS, in-app) for 15K active users\n"
        "• Technologies: Python, Django, React, Redis, Celery\n\n"
        "EDUCATION\n"
        "B.S. Computer Science — University of California, Berkeley (2012 – 2016)\n\n"
        "SKILLS\n"
        "Python, FastAPI, Django, PostgreSQL, Kafka, Kubernetes, Docker, AWS, "
        "TypeScript, React, CI/CD, System Design"
    ),
)
