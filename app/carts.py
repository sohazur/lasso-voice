"""Per-call cart context.

The script tag (Phase C) captures the live cart at checkout abandonment and POSTs it
to /consent, which stashes it here under a generated call_id. /twiml passes that
call_id into the media stream as a <Parameter>, so run_bot can load the SHOPPER's REAL
cart and the agent talks about actual items + total instead of "a few items".

In-memory only — a demo's worth of carts. Swap for Supabase later without touching the
call path (CartStore is the single seam).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CartItem:
    title: str
    qty: int = 1
    price: float = 0.0  # unit price


@dataclass
class Cart:
    """One abandoned cart tied to one outbound call."""

    call_id: str
    phone: str
    store_name: str = "the store"
    customer_name: str = "there"
    currency: str = "USD"
    items: list[CartItem] = field(default_factory=list)
    total: float = 0.0
    objection_hint: str | None = None  # optional: what we expect them to push back on

    def summary(self) -> str:
        """A short, phone-friendly description of the cart (no codes/URLs)."""
        if not self.items:
            return "a few items"
        parts = [f"{i.qty}× {i.title}" if i.qty > 1 else i.title for i in self.items[:3]]
        more = len(self.items) - 3
        if more > 0:
            parts.append(f"and {more} more")
        return ", ".join(parts)

    def total_str(self) -> str:
        sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(self.currency.upper(), "")
        return f"{sym}{self.total:,.2f}" if sym else f"{self.total:,.2f} {self.currency}"


class CartStore:
    """In-memory call_id -> Cart. One instance shared by the server."""

    def __init__(self) -> None:
        self._carts: dict[str, Cart] = {}

    def put(self, cart: Cart) -> None:
        self._carts[cart.call_id] = cart

    def get(self, call_id: str | None) -> Cart | None:
        if not call_id:
            return None
        return self._carts.get(call_id)

    def __len__(self) -> int:
        return len(self._carts)
