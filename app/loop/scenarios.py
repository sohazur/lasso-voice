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
        "You are a shopper who left a cart at checkout. You ALREADY believe the product is "
        "high quality — quality is NOT your concern, so praising the craftsmanship does "
        "nothing for you. Your problem is the out-the-door PRICE versus your budget right "
        "now: 'I know it's good, it's just more than I can spend today.' You will complete "
        "the purchase ONLY if the agent gives you a concrete way to lower what you pay — a "
        "discount code, free shipping, a bundle deal, or a payment plan. If the agent only "
        "talks up quality/value without a real price lever, you politely decline and leave."
    ),
    expected_outcome=(
        "Agent offers a CONCRETE PRICE LEVER — a discount code, free shipping, a bundle, or "
        "installments — not just quality reassurance. The shopper ends willing to complete. "
        "If the agent only defends the price with quality talk and offers no lever, FAIL."
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
