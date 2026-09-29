"""Routing labels from a Zendesk export (app/zendesk_labels.py), on a hand-built export."""
from app import zendesk_labels as Z

A, B, C, GEN = 1, 2, 3, 9
AG1, AG2, CUST, ORG = 10, 11, 20, 500
T0 = "2026-04-01T09:00:00Z"


def at(h: float) -> str:
    return Z.datetime.fromtimestamp(Z.ts(T0) + h * 3600, Z.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mk(tid, moves, status="closed", tags=(), channel="email", solved=48.0, org=None, start=0.0, subject="Help", body="It broke."):
    """moves: [(hours after start, group, 'rule'|'person')]."""
    aud = [{"id": tid * 100, "ticket_id": tid, "created_at": at(start), "author_id": CUST, "via": {"channel": channel},
            "events": [{"type": "Create", "field_name": "subject", "value": subject},
                       {"type": "Create", "field_name": "tags", "value": ["web_form"]},
                       {"type": "Create", "field_name": "360010001", "value": "returns_refunds"},
                       {"type": "Comment", "public": True, "body": body}]}]
    prev = None
    for k, (h, g, by) in enumerate(moves):
        ev = {"type": "Change", "field_name": "group_id", "value": str(g), "previous_value": prev}
        if by == "rule":
            ev["via"] = {"channel": "rule", "source": {"rel": "trigger"}}
        aud.append({"id": tid * 100 + k + 1, "ticket_id": tid, "created_at": at(start + h), "author_id": -1 if by == "rule" else AG1,
                    "via": {"channel": "rule" if by == "rule" else "web"}, "events": [ev]})
        prev = str(g)
    # later edits that name the answer: must not reach the snapshot
    aud.append({"id": tid * 100 + 50, "ticket_id": tid, "created_at": at(start + 5), "author_id": AG2, "via": {"channel": "web"},
                "events": [{"type": "Change", "field_name": "subject", "value": "[B] " + subject, "previous_value": subject},
                           {"type": "Change", "field_name": "tags", "value": ["web_form", "refund_processed"], "previous_value": ["web_form"]}]})
    if status in ("solved", "closed"):
        aud.append({"id": tid * 100 + 60, "ticket_id": tid, "created_at": at(start + solved), "author_id": AG2, "via": {"channel": "web"},
                    "events": [{"type": "Change", "field_name": "status", "value": "solved", "previous_value": "open"}]})
    final = moves[-1][1] if moves else None
    t = {"id": tid, "created_at": at(start), "via": {"channel": channel}, "subject": "[B] " + subject, "raw_subject": subject,
         "status": status, "group_id": final, "tags": ["web_form", "refund_processed", *tags], "organization_id": org}
    return t, aud


def build(cases, **kw):
    tickets, audits = [], []
    for t, a in cases:
        tickets.append(t)
        audits += a
    users = [{"id": AG1, "name": "Ana", "role": "agent"}, {"id": AG2, "name": "Bo", "role": "agent"}, {"id": CUST, "name": "C", "role": "end-user"}]
    orgs = [{"id": ORG, "organization_fields": {"plan": "Enterprise", "contract_term": "annual", "renewal_date": "2027-03-01"}}]
    groups = [{"id": A, "name": "A"}, {"id": B, "name": "B"}, {"id": C, "name": "C"}, {"id": GEN, "name": "General"}]
    rows, rep = Z.build(tickets, audits, users, orgs, groups, **kw)
    return {int(r["id"]): r for r in rows}, rep


def test_tiers_and_snapshot():
    rows, rep = build([
        mk(1, [(0, A, "rule")]),                                       # trigger, stood
        mk(2, [(1, A, "person"), (3, B, "person")], org=ORG),          # corrected within hours
        mk(3, [(1, A, "person"), (31, B, "person")], org=ORG, start=2),  # corrected late; ticket 2 still open
        mk(4, [(0.1, GEN, "person")], channel="chat", solved=0.2),     # answered in the chat
        mk(5, [(1, A, "person")], tags=("closed_by_merge",)),
        mk(6, [], status="deleted"),
        mk(7, [(1, A, "person"), (2, B, "person"), (3, C, "person")]),  # contested
        mk(8, [(1, A, "person"), (2, GEN, "person")]),                 # moved into the catch-all
        mk(9, [(1, A, "person")], status="open"),
        mk(10, []),                                                    # solved without ever getting a group
    ])
    tier = {k: (r["tier"], r["tier_reason"]) for k, r in rows.items()}
    assert tier[1] == ("medium", "first assignment stood (rule)")
    assert tier[2] == ("strong", "corrected")
    assert tier[3] == ("medium", "late correction")
    assert tier[4] == ("excluded", "answered at first contact")
    assert tier[5] == ("excluded", "merged") and tier[6] == ("excluded", "deleted")
    assert tier[7] == ("weak", "contested") and tier[8] == ("excluded", "moved into a catch-all")
    assert tier[9] == ("excluded", "open at export") and tier[10] == ("excluded", "never routed")
    r = rows[2]
    assert r["subject"] == "Help" and r["message"] == "It broke." and r["label"] == "B"          # the customer's words, not the agent's edits
    assert r["field_360010001"] == "returns_refunds" and "refund_processed" not in str(r)
    assert r["first_group"] == "A" and r["first_by"] == "person" and r["first_agent"] == "Ana"
    assert rows[2]["org_open_tickets"] == 0 and rows[3]["org_open_tickets"] == 1                # 2 was still open when 3 arrived
    assert rows[2]["days_to_renewal"] == 333 and rows[1]["days_to_renewal"] is None             # annual renewal stepped back a year
    assert rep["kept"] == 3 and rep["first_assignment_agrees"]["person"] == {"right": 0, "n": 2, "rate": 0.0}
    assert rep["label_first_seen"] == {"A": "2026-04-01", "B": "2026-04-01"}


def test_settings():
    rows, _ = build([mk(3, [(1, A, "person"), (31, B, "person")])], hours=48)
    assert rows[3]["tier"] == "strong"                                                           # 30 h is within 48
    rows, _ = build([mk(8, [(1, A, "person"), (2, GEN, "person")])], catch_all=())
    assert rows[8]["tier"] == "strong"                                                           # no catch-all named


def test_roast():
    from app import zendesk_roast as RR
    rows, _ = build([
        mk(1, [(0, A, "rule")]), mk(2, [(1, A, "person"), (3, B, "person")], org=ORG),
        mk(3, [(1, A, "person"), (31, B, "person")], org=ORG, start=2), mk(11, [(1, B, "person")]),
        mk(4, [(0.1, GEN, "person")], channel="chat", solved=0.2), mk(6, [], status="deleted"),
    ])
    rep = RR.analyze(list(rows.values()), agent_min=1)
    assert rep["kept"] == 4 and rep["first_right"]["people"] == 1 / 3 and rep["first_right"]["triggers"] == 1.0
    assert rep["moved"]["n"] == 2 and rep["moved"]["hours"] == 2 + 30                       # A for 2 h, then A for 30 h
    assert rep["pairs"][0] == {"from": "A", "to": "B", "n": 2, "hours": 32.0, "median_h": 16.0}
    assert rep["excluded"] == {"answered at first contact": 1, "deleted": 1}
    assert RR.hours_wrong({"label": "B", "moves": [[1, "A", "person"], [2, "C", "person"], [5, "B", "person"]]}) == 4
    page = RR.render(rep, "Acme")
    assert page.startswith("<title>Acme Routing Review</title>") and "33%" in page and "upper bound" in page
