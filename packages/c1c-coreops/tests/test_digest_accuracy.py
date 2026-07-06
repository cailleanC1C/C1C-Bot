from datetime import datetime, timezone

from c1c_coreops.cog import CoreOpsCog
from c1c_coreops import cog as coreops_cog
from c1c_coreops.render import DigestSheetEntry, _maybe_build_tip


def _cog():
    return object.__new__(CoreOpsCog)


def test_digest_bucket_success_ignores_stale_last_error(monkeypatch):
    monkeypatch.setattr(coreops_cog, "_list_bucket_names", lambda: {"clans"})
    monkeypatch.setattr(
        coreops_cog,
        "_gather_snapshot_dicts",
        lambda names: {
            "clans": {
                "available": True,
                "last_result": "ok",
                "last_error": "old _retry_with_backoff must not run inside an active event loop",
                "last_refresh_at": datetime.now(timezone.utc),
            }
        },
    )

    entries = _cog()._collect_sheet_bucket_entries()
    clan = next(entry for entry in entries if entry.display_name == "ClanInfo")

    assert clan.status == "ok"
    assert clan.error == "—"


def test_digest_sheets_client_success_ignores_stale_global_error(monkeypatch):
    monkeypatch.setattr(coreops_cog, "_list_bucket_names", lambda: {"fusion"})
    monkeypatch.setattr(
        coreops_cog,
        "_gather_snapshot_dicts",
        lambda names: {
            "fusion": {
                "available": True,
                "last_result": "ok",
                "last_error": "FUSION_TAB missing in milestones Config tab",
                "last_refresh_at": datetime.now(timezone.utc),
                "last_latency_ms": 12,
                "retries": 0,
            }
        },
    )

    summary = _cog()._collect_sheets_client_summary(datetime.now(timezone.utc))

    assert summary is not None
    assert summary.last_error is None


def test_digest_tip_targets_failing_bucket_not_claninfo_for_templates_or_code_error():
    templates = [
        DigestSheetEntry(
            display_name="Templates",
            status="fail",
            age_seconds=None,
            next_refresh_delta_seconds=None,
            next_refresh_at=None,
            retries=1,
            error="template sheet failed",
        )
    ]
    guard = [
        DigestSheetEntry(
            display_name="ClanInfo",
            status="fail",
            age_seconds=None,
            next_refresh_delta_seconds=None,
            next_refresh_at=None,
            retries=1,
            error="_retry_with_backoff must not run inside an active event loop; use the async variant",
        )
    ]

    assert "templates" in (_maybe_build_tip(templates) or "").lower()
    assert "clansinfo" not in (_maybe_build_tip(templates) or "").lower()
    assert "code attention" in (_maybe_build_tip(guard) or "").lower()
