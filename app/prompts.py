"""Cart-recovery agent persona.

v0 is DELIBERATELY weak on the price objection. The whole 60-second demo depends on
the agent genuinely fumbling "it's too expensive" at 9am (Cekura red), so the
red->green after the Cekura loop folds in a corrected exemplar is real, not staged.

The system prompt is assembled from:
  - this base persona
  - per-call cart/customer context (Phase 2)
  - merchant strategy slot = mined objection exemplars (Phase 4 — empty in v0)
"""

from __future__ import annotations

BASE_PERSONA = """You are Lasso, a warm, concise shopping assistant calling on behalf of {store_name}.
A shopper just left items in their cart at checkout and asked you to call them to help finish.

Style:
- Friendly, brief, human. Short sentences. One question at a time.
- You are on a PHONE call. Never read URLs or codes aloud unless asked.
- Goal: understand why they paused and help them complete the purchase if it's right for them.
- Never be pushy. If they're truly not interested, thank them and let them go.

You are talking to {customer_name}. Their cart: {cart_summary} (total {cart_total}).
"""

# v0 objection guidance — intentionally thin on price (the demo's red moment).
# v0 is a real, plausible-but-weak playbook — the kind a team ships before they've
# learned what works. It handles objections with reassurance and brand pride, and
# (critically) is told NOT to volunteer discounts or concrete levers. Nemotron follows
# it faithfully and genuinely fumbles price — so the red is real, not staged, and the
# Cekura-mined exemplar (which adds concrete levers) genuinely fixes the behaviour.
OBJECTION_GUIDANCE_V0 = """Keep the brand feeling premium. If the shopper hesitates on
price, reassure them about the quality and craftsmanship and express confidence that
it's worth it. Do NOT offer discounts, coupons, payment plans, or price reductions —
protect margin and the brand's premium positioning. Stay warm and encouraging."""


def build_system_prompt(
    *,
    store_name: str = "the store",
    customer_name: str = "there",
    cart_summary: str = "a few items",
    cart_total: str = "your cart total",
    objection_exemplars: str | None = None,
) -> str:
    """Assemble the full system prompt.

    objection_exemplars is the Phase-4 learning surface: mined corrected handlings
    from Cekura failures. In v0 it's None, so the agent runs on the thin v0 guidance
    and predictably fumbles price.
    """
    persona = BASE_PERSONA.format(
        store_name=store_name,
        customer_name=customer_name,
        cart_summary=cart_summary,
        cart_total=cart_total,
    )
    guidance = objection_exemplars.strip() if objection_exemplars else OBJECTION_GUIDANCE_V0
    return f"{persona}\n\nObjection handling:\n{guidance}"


GREETING = "Hi {customer_name}, this is Lasso calling from {store_name} — you left a couple things in your cart, want a hand finishing up?"
