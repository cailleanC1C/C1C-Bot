import asyncio

from modules.onboarding import watcher_welcome
from shared.sheets import onboarding as onboarding_sheets


def test_finalization_headers_resolve_from_config(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_CONFIG_CACHE",
        {
            "welcome_finalization_status_header": "finalization_status",
            "welcome_reservation_status_header": "reservation_status",
            "welcome_clan_update_status_header": "clan_update_status",
            "welcome_finalization_note_header": "finalization_note",
        },
    )
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    headers = onboarding_sheets.get_welcome_headers()

    assert headers[-4:] == [
        "finalization_status",
        "reservation_status",
        "clan_update_status",
        "finalization_note",
    ]


def test_finalization_state_reads_configured_headers(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_CONFIG_CACHE",
        {
            "promo_finalization_status_header": "finalization_status",
            "promo_reservation_status_header": "reservation_status",
            "promo_clan_update_status_header": "clan_update_status",
            "promo_finalization_note_header": "finalization_note",
            "promo_source_clan_tag_header": "source_clan_tag",
        },
    )
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    state = onboarding_sheets.get_ticket_finalization_state(
        "promo",
        {
            "finalization_status": "done",
            "reservation_status": "released",
            "clan_update_status": "done",
            "finalization_note": "finalized by Ticket Tool close",
        },
    )

    assert state == {
        "finalization_status": "done",
        "reservation_status": "released",
        "clan_update_status": "done",
        "finalization_note": "finalized by Ticket Tool close",
    }


def test_discord_placement_log_is_concise(monkeypatch):
    sent = []

    async def fake_send(message):
        sent.append(message)

    monkeypatch.setattr(watcher_welcome.rt, "send_log_message", fake_send)

    asyncio.run(
        watcher_welcome._send_placement_log_line(
            flow="promo",
            outcome="success",
            ticket="M0354",
            player="Xaereth",
            source="C1CB",
            destination="C1CD",
            trigger="ticket_tool",
            reservation="none",
            clan_update="done",
            finalization_status="done",
        )
    )

    assert sent == [
        "✅ placement finalized • flow=promo • ticket=M0354 • player=Xaereth • C1CB→C1CD • reservation=none • clan_update=done • trigger=ticket_tool • finalization=done"
    ]
    assert "Traceback" not in sent[0]
    assert "{" not in sent[0]
