"""Synthetic data for the entitlement-routing case (EXTERNAL-STEPS.md §1).

Writes two files and nothing else:

  records.json                                     what the records service serves: customer_id -> record
  ../../studio/sample/tickets-entitlement.jsonl    the labelled dataset: ticket_id, message, customer_id, queue

The labels are generated from a rule that reads BOTH the message and the record, so a prompt that never sees the
record has a ceiling it cannot pass at any length. That is the whole point of the case; if the rule were a function
of the message alone, the external step would be decorative and the demo would prove nothing.

Deterministic: same seed, same files. Run it again only if you mean to change the dataset.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20260923

# ---- messages: 20 per intent, built from phrasings x slots --------------------------------------

PHRASES = {
    "bug": [
        "The app crashes every time I open {thing}.",
        "{thing} has been returning a 500 error since yesterday.",
        "Notifications stopped arriving after the latest update.",
        "The export button does nothing on {thing} - no file, no error.",
        "Search results are empty even though I know the records exist.",
        "The dashboard shows last week's numbers and will not refresh.",
        "Two-factor codes never arrive, so I am locked out of {thing}.",
        "Uploading a file over 10MB fails silently every time.",
        "The mobile app logs me out every few minutes.",
        "Timestamps are showing in the wrong timezone across {thing}.",
    ],
    "refund": [
        "The {product} does not fit. How do I return it for a refund?",
        "Wrong item shipped - I ordered blue, got red. Just refund it.",
        "The course was not what I expected. Can I get a refund?",
        "I returned the {product} two weeks ago and still have no refund.",
        "Order {order} never showed up. Please refund me.",
        "Item arrived three weeks late so I no longer need it. Refund please.",
        "We are not using this any more. Please cancel and refund the remainder.",
        "This is not what was demoed to us. We would like our money back.",
        "The {product} broke after a week. I want a refund, not a replacement.",
        "Please refund order {order}, I bought the same thing elsewhere.",
    ],
    "billing": [
        "I was charged twice for order {order} this month.",
        "You charged my card after I downgraded to the free plan.",
        "My invoice shows {n} seats but we only have {m}.",
        "The VAT on invoice {order} looks wrong for our country.",
        "Can you send a copy of last quarter's invoices?",
        "The card on file expired - how do I update it?",
        "We were billed annually but agreed to monthly.",
        "There is a line item on this invoice I do not recognise.",
        "Why did my price go up this month?",
        "Please switch our billing contact to our finance team.",
    ],
    "account": [
        "I cannot log in - it says my email is not recognised.",
        "Please add {n} more users to our workspace.",
        "How do I transfer ownership of the account to a colleague?",
        "I need to reset my password but the email never arrives.",
        "Can you merge these two accounts we created by mistake?",
        "Please remove a former employee's access immediately.",
        "How do I change the email address on my account?",
        "We need SSO enabled for our domain.",
        "My colleague cannot see the shared workspace.",
        "Please delete my account and all my data.",
    ],
    "shipping": [
        "Order {order} says delivered but nothing arrived.",
        "Can I change the delivery address on order {order}?",
        "The tracking number for my {product} has not updated in a week.",
        "Do you ship to Ireland, and how long does it take?",
        "The parcel arrived damaged - the box was crushed.",
        "Can I get order {order} sent express instead?",
        "I missed the delivery. How do I arrange redelivery?",
        "Half of order {order} arrived - where is the rest?",
        "Can you hold the shipment until next week?",
        "The courier left the package outside in the rain.",
    ],
    "other": [
        "Do you have a student discount?",
        "Is there an API for {thing}?",
        "Where can I find your privacy policy?",
        "Do you offer training for new teams?",
        "Can I speak to someone about a partnership?",
        "What is your uptime guarantee?",
        "Is {thing} available in Spanish?",
        "Are you hiring support engineers?",
        "Can I get a copy of your SOC 2 report?",
        "Do you have a roadmap I can look at?",
    ],
}
THINGS = ["the reports page", "the inbox", "the admin panel", "the mobile app", "the API", "the billing page"]
PRODUCTS = ["headphones", "jacket", "standing desk", "keyboard", "monitor stand", "office chair"]


def messages(rnd: random.Random) -> list[tuple[str, str]]:
    out = []
    for intent, phrases in PHRASES.items():
        seen = set()
        while len(seen) < 20:
            p = rnd.choice(phrases)
            m = p.format(thing=rnd.choice(THINGS), product=rnd.choice(PRODUCTS), order=rnd.randint(1000, 9999),
                         n=rnd.randint(8, 40), m=rnd.randint(3, 7))
            if m not in seen:
                seen.add(m)
                out.append((intent, m))
    return out


# ---- customers ----------------------------------------------------------------------------------

def customers(rnd: random.Random, n: int = 70) -> dict[str, dict]:
    recs = {}
    for i in range(n):
        cid = f"C{1000 + i}"
        plan = rnd.choices(["free", "pro", "enterprise"], weights=[3, 4, 3])[0]
        contract = plan == "enterprise" or (plan == "pro" and rnd.random() < 0.5)
        recs[cid] = {
            "customer_id": cid,
            "plan": plan,
            "tenure_days": rnd.randint(10, 2200),
            "open_tickets": rnd.choices([0, 1, 2, 3, 4], weights=[4, 3, 2, 1, 1])[0],
            "contract": contract,
            "renewal_days": rnd.randint(5, 330) if contract else 0,
        }
    return recs


# ---- the labelling rule (reads message intent AND record) ----------------------------------------

def queue(intent: str, rec: dict | None) -> str:
    """No record -> fall back to the message-only queue. That is the honest behaviour: an unknown customer is not an
    escalation, and roughly 8% of rows have no record, so coverage is something validation can actually measure."""
    if intent == "bug":
        if rec and rec["plan"] == "enterprise" and rec["open_tickets"] >= 2:
            return "escalation"
        return "bug"
    if intent == "refund":
        if rec and rec["contract"] and rec["renewal_days"] <= 60:
            return "retention"
        return "refund"
    return intent


def main() -> None:
    rnd = random.Random(SEED)
    recs = customers(rnd)
    ids = list(recs)
    msgs = messages(rnd)
    rnd.shuffle(msgs)

    # pick customers so the joint classes are well represented: about a quarter of rows should need the record
    esc = [c for c in ids if recs[c]["plan"] == "enterprise" and recs[c]["open_tickets"] >= 2]
    ret = [c for c in ids if recs[c]["contract"] and recs[c]["renewal_days"] <= 60]
    rows = []
    for i, (intent, m) in enumerate(msgs):
        # ~half of bug / refund rows get a customer that flips them to escalation / retention. Half is deliberate:
        # the measurable quantity is the ACCURACY CEILING without the record, and a 50/50 split inside an intent is
        # where guessing the majority helps least - it maximises the gap the demo is there to show.
        if intent == "bug" and rnd.random() < 0.45:
            cid = rnd.choice(esc)
        elif intent == "refund" and rnd.random() < 0.45:
            cid = rnd.choice(ret)
        elif rnd.random() < 0.08:
            cid = f"C9{rnd.randint(100, 999)}"      # no record: the coverage case
        else:
            cid = rnd.choice(ids)
        rows.append({"ticket_id": f"T{2000 + i}", "message": m, "customer_id": cid,
                     "queue": queue(intent, recs.get(cid))})

    (HERE / "records.json").write_text(json.dumps(recs, indent=1))
    out = HERE.parent.parent / "studio" / "sample" / "tickets-entitlement.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    from collections import Counter
    print(f"{len(recs)} customers -> records.json")
    print(f"{len(rows)} tickets -> {out}")
    print("queues:", dict(Counter(r["queue"] for r in rows)))
    print("no record:", sum(1 for r in rows if r["customer_id"] not in recs))
    needs = sum(1 for r in rows if r["queue"] in ("escalation", "retention"))
    print(f"rows the message alone cannot classify: {needs} ({needs / len(rows):.0%})")
    # the number the demo turns on: with no record, the best any prompt can do inside an ambiguous intent is answer
    # its majority class, so the ceiling is (unambiguous rows + the larger side of each ambiguous pair) / all rows
    pairs = [("bug", "escalation"), ("refund", "retention")]
    amb = sum(sum(1 for r in rows if r["queue"] in p) for p in pairs)
    best_blind = sum(max(sum(1 for r in rows if r["queue"] == a), sum(1 for r in rows if r["queue"] == b)) for a, b in pairs)
    print(f"accuracy ceiling without the record: {(len(rows) - amb + best_blind) / len(rows):.0%} (with it: 100%)")


if __name__ == "__main__":
    main()
