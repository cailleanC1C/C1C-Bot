from datetime import datetime, timedelta, timezone

from cogs import app_admin


class _DummyJob:
    def __init__(self, name: str, component: str) -> None:
        self.name = name
        self.component = component
        self.interval = timedelta(minutes=5)
        self.next_run = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.tag = None


class _DueJob:
    name = "achievement_collector"
    component = "housekeeping"
    next_run = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
    tag = "housekeeping"
    cadence_label = "scheduled"


def _runtime_with_jobs(jobs):
    class _Scheduler:
        def __init__(self, jobs):
            self.jobs = jobs

    class _Runtime:
        def __init__(self, jobs):
            self.scheduler = _Scheduler(jobs)

    return _Runtime(jobs)


def _embed_text(embeds):
    chunks = []
    for embed in embeds:
        chunks.append(embed.title or "")
        chunks.append(embed.description or "")
        for field in embed.fields:
            chunks.append(field.name)
            chunks.append(field.value)
    return "\n".join(chunks)


def test_build_scheduler_overview_groups_components():
    runtime = _runtime_with_jobs(
        [
            _DummyJob("onboarding_idle_watcher", "recruitment"),
            _DummyJob("cache_refresh", "default"),
        ]
    )

    message = _embed_text(app_admin._build_scheduler_embeds(runtime, None))

    assert "Recruitment" in message
    assert "onboarding_idle_watcher" in message
    assert "cache_refresh" in message


def test_build_scheduler_overview_filters_components():
    runtime = _runtime_with_jobs(
        [
            _DummyJob("onboarding_idle_watcher", "recruitment"),
            _DummyJob("cache_refresh", "default"),
        ]
    )

    message = _embed_text(app_admin._build_scheduler_embeds(runtime, "recruitment"))

    assert "onboarding_idle_watcher" in message
    assert "cache_refresh" not in message


def test_build_scheduler_overview_handles_empty_filter():
    runtime = _runtime_with_jobs([])

    message = _embed_text(app_admin._build_scheduler_embeds(runtime, "unknown"))

    assert "No scheduled jobs under unknown." in message


def test_build_scheduler_overview_formats_due_job_without_unknown_interval():
    message = _embed_text(
        app_admin._build_scheduler_embeds(_runtime_with_jobs([_DueJob()]), None)
    )

    assert "Housekeeping" in message
    assert "achievement_collector" in message
    assert "next: 2026-07-20 09:30 UTC (scheduled)" in message
    assert "every ?" not in message
