import asyncio

from shared.sheets import fusion


def test_reminder_dedupe_metadata_does_not_read_stale_cfg(monkeypatch):
    def stale_cfg_get(*args, **kwargs):
        raise AssertionError("stale cfg.get used")

    async def resolved(key):
        assert key == "FUSION_REMINDER_TAB"
        return "FusionReminderLog"

    class StaleCfg:
        def get(self, *args, **kwargs):
            return stale_cfg_get(*args, **kwargs)

    monkeypatch.setattr(fusion, "cfg", StaleCfg())
    monkeypatch.setattr(fusion, "_resolve_tab_name", resolved)

    sync_meta = fusion.reminder_dedupe_backend_metadata()
    async_meta = asyncio.run(fusion.areminder_dedupe_backend_metadata())

    assert sync_meta == {"backend_type": "google_sheets", "config_key": "FUSION_REMINDER_TAB"}
    assert async_meta["tab_name"] == "FusionReminderLog"
