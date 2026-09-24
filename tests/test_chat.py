from src.chat import Correction, build_queue, parse_correction

MARIE = {"id": 1, "name": "Marie Deysel", "company": "Elvey", "employment_status": "unknown"}
BOB = {"id": 2, "name": "Bob Jones", "company": "Acme Security (Pty) Ltd", "employment_status": "moved"}
ACCOUNTS = [{"id": 10, "name": "Beta Integrators"}, {"id": 11, "name": "Acme Security (Pty) Ltd"}]


def test_linkedin_url_pins_profile():
    r = parse_correction("https://www.linkedin.com/in/marie-deysel-1503a53a/", MARIE)
    assert r.action == "apply"
    assert r.payload == {"linkedin_url": "https://www.linkedin.com/in/marie-deysel-1503a53a/"}
    assert "Marie Deysel" in r.reply and "Re-queued" in r.reply

    # a URL embedded in a longer sentence still works
    r2 = parse_correction("try https://www.linkedin.com/in/marie-deysel-1503a53a/ for her",
                          MARIE)
    assert r2.payload["linkedin_url"] == "https://www.linkedin.com/in/marie-deysel-1503a53a/"

    # not a linkedin URL -> falls through, not misread as a link
    r3 = parse_correction("https://example.com/marie", MARIE)
    assert r3.action == "unclear"


def test_left_and_not_relevant():
    for text in ("left", "she left", "no longer there", "resigned", "not with the company anymore"):
        r = parse_correction(text, MARIE)
        assert r.action == "apply" and r.payload["contact_status"] == "left", text

    r = parse_correction("left in March, replaced by someone else", MARIE)
    assert r.payload["contact_status"] == "left"
    assert r.payload["status_note"] and "March" in r.payload["status_note"]

    for text in ("not relevant", "irrelevant", "n/a", "skip her"):
        r = parse_correction(text, MARIE)
        assert r.action == "apply" and r.payload["contact_status"] == "not_relevant", text


def test_active_confirms_or_undoes_a_flagged_move():
    r = parse_correction("active", MARIE)  # employment_status unknown -> plain confirm
    assert r.payload == {"contact_status": "active"}

    r2 = parse_correction("that's right, keep her", BOB)  # employment_status 'moved' -> undo the flag
    assert r2.payload == {"keep_account": True}
    assert "wrong" in r2.reply


def test_department_by_bare_word_or_label():
    for text in ("finance", "Finance", "department: Sales", "department - technical"):
        r = parse_correction(text, MARIE)
        assert r.action == "apply" and r.payload["department"] in ("Finance", "Sales", "Technical"), text
    assert parse_correction("astrology", MARIE).action == "unclear"


def test_move_to_existing_or_new_account():
    r = parse_correction("now at Beta Integrators", MARIE, ACCOUNTS)
    assert r.payload == {"move_to_account_id": 10}
    assert "Beta Integrators" in r.reply

    r2 = parse_correction("moved to Zeta Security", MARIE, ACCOUNTS)
    assert r2.payload == {"create_account": "Zeta Security"}
    assert "isn't in the database yet" in r2.reply

    r3 = parse_correction("-> Acme Security", MARIE, ACCOUNTS)  # fuzzy match against the (Pty) Ltd name
    assert r3.payload == {"move_to_account_id": 11}

    r4 = parse_correction("now at", MARIE, ACCOUNTS)
    assert r4.action == "unclear"


def test_skip_and_stop_apply_nothing():
    for text in ("skip", "Skip", "next", " pass "):
        r = parse_correction(text, MARIE)
        assert r.action == "skip" and r.payload is None
    for text in ("stop", "done", "quit"):
        r = parse_correction(text, MARIE)
        assert r.action == "stop" and r.payload is None


def test_unrecognised_text_asks_never_guesses():
    r = parse_correction("hmm not sure what's going on here", MARIE)
    assert r.action == "unclear" and r.payload is None
    assert parse_correction("", MARIE).action == "unclear"
    assert parse_correction("   ", MARIE).action == "unclear"


def test_build_queue_orders_no_profile_first_then_flags(cfg, loaded):
    from src import db as dbmod
    marie_id = dbmod.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")["id"]
    bob_id = dbmod.one(loaded, "SELECT id FROM contacts WHERE full_name='Bob Jones'")["id"]
    loaded.execute("UPDATE contacts SET enrich_status='no_profile' WHERE id=?", [marie_id])
    loaded.execute("UPDATE contacts SET enrich_status='done', role='Buyer', linkedin_summary='x', "
                   "linkedin_experience='[]', extra=? WHERE id=?",
                   [dbmod.jdump({"match_confidence": "name-only"}), bob_id])
    loaded.commit()
    q = build_queue(loaded, cfg)
    ids = [r["id"] for r in q]
    assert ids.index(marie_id) < ids.index(bob_id)  # no_profile ranks ahead of unsure_match
    assert "couldn't find" in next(r for r in q if r["id"] == marie_id)["reason"]
    assert "name only" in next(r for r in q if r["id"] == bob_id)["reason"]
    assert len(ids) == len(set(ids))  # no duplicates even if a contact matched more than one check
