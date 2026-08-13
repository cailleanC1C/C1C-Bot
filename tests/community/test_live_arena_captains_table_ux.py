from modules.community.live_arena import full_set_scoring


def test_captains_table_uses_friendly_labels_and_rows():
    assert full_set_scoring._FRIENDLY_LABELS["View Roster"] == "View Players"
    assert full_set_scoring._FRIENDLY_LABELS["Close Current Round"] == "Finish Round"
    assert full_set_scoring._FRIENDLY_LABELS["Review Result Issues"] == "Review Match Issues"
    assert full_set_scoring._FRIENDLY_LABELS["Competition Ops"] == "Organizer Actions"
    assert full_set_scoring._FRIENDLY_ROWS["Finish Round"] == 0
    assert full_set_scoring._FRIENDLY_ROWS["Review Match Issues"] == 1
    assert full_set_scoring._FRIENDLY_ROWS["View Players"] == 2
    assert full_set_scoring._FRIENDLY_ROWS["Repair Tournament"] == 3
