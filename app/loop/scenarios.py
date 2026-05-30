"""Abandoning-shopper scenarios.

`price` is the locked demo scenario — the agent's v0 prompt is intentionally weak on
it, so the red->green is real. The others exist so the system demonstrably generalizes
("everything, over time"); they are not part of the 60s arc.
"""

from __future__ import annotations

from .types import Scenario

PRICE = Scenario(
    objection="price",
    persona=(
        "You are a shopper who left a cart at checkout. You like the product but you "
        "think it's too expensive and say so directly: 'honestly it's just a bit too "
        "pricey for me right now.' You are not hostile, just price-sensitive. You will "
        "complete the purchase ONLY if the agent gives you a concrete, satisfying reason "
        "the price is worth it OR a real way to lower it (discount, bundle, payment plan). "
        "Vague reassurance ('it's great quality!') does NOT convince you — push back once "
        "more if you only get fluff."
    ),
    expected_outcome=(
        "Agent acknowledges the price concern specifically and responds with a CONCRETE "
        "value justification or a concrete lever (discount code, free shipping, bundle, "
        "or installments) — not generic reassurance. The shopper ends willing to complete."
    ),
)

SHIPPING = Scenario(
    objection="shipping",
    persona=(
        "You abandoned because shipping felt slow/expensive. You'll buy if the agent gives "
        "a concrete shipping fix (faster option, free-shipping threshold, exact ETA)."
    ),
    expected_outcome="Agent gives a concrete shipping remedy or accurate ETA; shopper is satisfied.",
)

BROWSING = Scenario(
    objection="browsing",
    persona=(
        "You were 'just browsing' and not ready to buy. You'll engage if the agent is "
        "low-pressure and offers a genuinely useful reason to decide now (or to save the cart)."
    ),
    expected_outcome="Agent stays low-pressure, adds real value, and secures a next step without nagging.",
)

ALL: dict[str, Scenario] = {s.objection: s for s in (PRICE, SHIPPING, BROWSING)}


def get(objection: str) -> Scenario:
    if objection not in ALL:
        raise KeyError(f"unknown objection {objection!r}; have {list(ALL)}")
    return ALL[objection]
