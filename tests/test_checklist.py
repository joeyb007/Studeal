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


# ---- v2 criteria engine (send-to-scout v2, stage 1) -----------------------

from dealbot.api.routes.inspections import _apply_assessment, _criteria_from_payload


def _item(check="Check the cushions for cracks.", status="open", **kw):
    return {"check": check, "status": status, "evidence": None, "verify_via": None, **kw}


def test_criteria_payload_validates_and_caps():
    parsed = {"criteria": [
        {"check": "Ask for battery percentage after an hour.", "status": "open", "verify_via": "ask_seller"},
        {"check": "Photos show clean ear cups.", "status": "satisfied", "evidence": "photos", "verify_via": "confirmed"},
        {"check": "short", "status": "open"},                       # too short: dropped
        {"check": "Test ANC in both ears at pickup.", "status": "weird", "verify_via": "nope"},
        {"check": "A." * 10, "status": "open", "verify_via": "at_pickup"},
        {"check": "Sixth check that is over the cap of five items.", "status": "open"},
        {"check": "Seventh check beyond the cap entirely here too.", "status": "open"},
    ]}
    items = _criteria_from_payload(parsed)
    assert len(items) <= 5
    assert items[0]["verify_via"] == "ask_seller"
    assert items[1]["status"] == "satisfied" and items[1]["verify_via"] == "confirmed"
    assert all(i["check"] != "short" for i in items)
    assert items[2]["status"] == "open" and items[2]["verify_via"] is None  # bad enums degrade


def test_apply_assessment_settles_and_adds():
    items = [_item(), _item("Ask about the battery health percentage.")]
    parsed = {
        "updates": [{"index": 0, "status": "satisfied", "evidence": "photos show clean cushions"}],
        "additions": [{"check": "Crackling in right ear explained or tested.",
                       "status": "flagged", "evidence": "seller mentioned crackle",
                       "verify_via": "at_pickup"}],
    }
    out = _apply_assessment(items, parsed)
    assert out[0]["status"] == "satisfied"
    assert out[1]["status"] == "open"
    assert out[2]["added"] is True and out[2]["status"] == "flagged"
    assert out[2]["verify_via"] == "at_pickup"
    assert items[0]["status"] == "open"        # input untouched


def test_apply_assessment_bounds_additions():
    items = [_item()]
    additions = [{"check": f"Newly surfaced concern number {i} to verify.", "status": "open"} for i in range(5)]
    out = _apply_assessment(items, {"additions": additions})
    assert sum(1 for i in out if i.get("added")) == 3
    out2 = _apply_assessment(out, {"additions": additions})
    assert sum(1 for i in out2 if i.get("added")) == 3   # cap is per thread, not per call


def test_apply_assessment_never_touches_settled_checks():
    items = [_item(status="satisfied"), _item("Ask about the box.", status="flagged")]
    out = _apply_assessment(items, {"updates": [
        {"index": 0, "status": "flagged", "evidence": "x"},
        {"index": 1, "status": "satisfied", "evidence": "y"},
    ]})
    assert out[0]["status"] == "satisfied"
    assert out[1]["status"] == "flagged"


def test_tailoring_payload_validates():
    from dealbot.api.routes.inspections import _tailoring_from_payload
    assert _tailoring_from_payload({"tailoring": None}) is None
    assert _tailoring_from_payload({}) is None
    assert _tailoring_from_payload({"tailoring": {"question": "hm?"}}) is None  # too short
    t = _tailoring_from_payload({"tailoring": {
        "question": "Do you care how they look, or just how they sound?",
        "chips": ["looks matter", "just the sound", "", 42, "bit of both", "extra"],
    }})
    assert t["question"].startswith("Do you care")
    assert t["chips"] == ["looks matter", "just the sound", "bit of both"]
    assert t["answer"] is None
