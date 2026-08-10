"""Ready-to-buy checklist seams (copilot spec 2026-08-10, phase C)."""

from dealbot.api.routes.inspections import checklist_from_playbook

PLAYBOOK = """What to check
Press the ear cushions for cracks. Ask for battery percentage after an hour of use. Check the headband tension and look for fraying.
The going rate
Around $280 typically.
How to haggle
Open low, meet in the middle.
Your walk-away
$300."""


def test_checks_parse_one_per_sentence():
    items = checklist_from_playbook(PLAYBOOK)
    assert [i["check"] for i in items] == [
        "Press the ear cushions for cracks.",
        "Ask for battery percentage after an hour of use.",
        "Check the headband tension and look for fraying.",
    ]
    assert all(i["status"] == "open" and i["evidence"] is None for i in items)


def test_no_playbook_no_checks():
    assert checklist_from_playbook(None) == []
    assert checklist_from_playbook("How to haggle\nJust ask.") == []


def test_checks_capped_at_five():
    many = "What to check\n" + " ".join(
        f"Verify the number {i} thing on the item works." for i in range(9)
    ) + "\nThe going rate\nWhatever."
    assert len(checklist_from_playbook(many)) == 5


def test_tiny_fragments_dropped():
    playbook = "What to check\nYes. Press the cushions firmly for cracks.\nThe going rate\nX."
    items = checklist_from_playbook(playbook)
    assert [i["check"] for i in items] == ["Press the cushions firmly for cracks."]
