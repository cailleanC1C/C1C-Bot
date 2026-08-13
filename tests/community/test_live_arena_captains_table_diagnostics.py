from modules.community.live_arena import captains_table_quota_safe, tournament_lifecycle


def test_temporary_captains_table_diagnostics_no_longer_owns_lifecycle_sync():
    assert tournament_lifecycle._sync_organizer_panel.__module__ == tournament_lifecycle.__name__
    assert captains_table_quota_safe._installed is True
