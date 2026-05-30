"""Lasso Voice control plane (FastAPI).

Outbound call flow (the whole product is OUTBOUND, the hackathon starter is inbound-only):
  1. POST /dialout {to_number, call_id?}  -> Twilio client.calls.create(twiml_url=/twiml)
  2. Twilio dials the shopper; on answer fetches POST /twiml
  3. /twiml returns <Connect><Stream url="wss://.../ws"/> -> Twilio opens the media WS
  4. WS /ws -> parse_telephony_websocket -> run_bot() for the call's lifetime

Phase 1 places a call with a static cart-recovery persona. Phase 2 feeds per-call
context via the call_id; Phase 4 conditions the prompt on Cekura-mined exemplars.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel
from pipecat.runner.utils import parse_telephony_websocket
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from .bot import run_bot
from .carts import Cart, CartItem, CartStore
from .config import llm_label, load_settings, voice_label
from .loop.improve import ImprovementLoop
from .loop.store import LoopStore

settings = load_settings()
twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

app = FastAPI(title="Lasso Voice")

_STATIC = Path(__file__).parent / "static"
_DASHBOARD = _STATIC / "dashboard.html"
_EMBED_JS = _STATIC / "embed.js"
_DEMO_STORE = _STATIC / "demo_store.html"


@app.get("/")
async def dashboard_page():
    """The 'it learns' scoreboard — the demo's payoff screen."""
    return FileResponse(_DASHBOARD)


@app.get("/embed.js")
async def embed_js():
    """The embeddable cart-recovery script tag."""
    return FileResponse(_EMBED_JS, media_type="application/javascript")


@app.get("/demo-store")
async def demo_store():
    """A fake storefront with the Lasso tag on it — where we test abandonment."""
    return FileResponse(_DEMO_STORE)

# One shared store so /improve, /cekura/run and /api/scoreboard see the same state.
_store = LoopStore(settings)
# Per-call abandoned carts (script tag -> /consent -> here -> the call).
_carts = CartStore()
_loop: ImprovementLoop | None = None


def get_loop() -> ImprovementLoop:
    """Lazy-init: the loop needs an LLM key (Nemotron or OpenAI); /dialout does not.
    Building it lazily keeps the server bootable when only telephony keys are set."""
    global _loop
    if _loop is None:
        _loop = ImprovementLoop(settings, store=_store)
    return _loop


class DialoutRequest(BaseModel):
    to_number: str  # E.164, e.g. +14155551234
    call_id: str | None = None


class CartItemIn(BaseModel):
    title: str
    qty: int = 1
    price: float = 0.0


class ConsentRequest(BaseModel):
    """What the script tag POSTs when a shopper taps 'Call me to finish'."""

    store_id: str = "acme"
    store_name: str = "the store"
    phone: str  # E.164 — the consented shopper's number
    consent: bool = False  # must be explicitly true (TCPA-style gate)
    customer_name: str = "there"
    currency: str = "USD"
    items: list[CartItemIn] = []
    total: float = 0.0
    objection_hint: str | None = None


_PLACEHOLDER_BASE = "your-ngrok-subdomain"


@app.get("/health")
async def health():
    return {
        "ok": True,
        "llm": llm_label(settings),
        "voice": voice_label(settings),
        "from_number": settings.twilio_from_number,
        "ws_url": settings.ws_url,
        "cekura_configured": bool(settings.cekura_api_key),
        "public_base_url_set": _PLACEHOLDER_BASE not in settings.public_base_url,
    }


@app.get("/api/config")
async def api_config():
    """Public base URL + snippet for the dashboard's copy-paste box (C3)."""
    base = settings.public_base_url.rstrip("/")
    return {
        "public_base_url": base,
        "snippet": f'<script src="{base}/embed.js" data-store="acme" data-store-name="Acme Coffee"></script>',
    }


def _place_call(to_number: str, call_id: str | None) -> JSONResponse:
    """Place one outbound call; surface Twilio's real rejection reason on failure.

    Shared by /dialout (raw) and /consent (cart-driven) so the call path is identical.
    """
    twiml_url = f"{settings.public_base_url.rstrip('/')}/twiml"
    if call_id:
        twiml_url += f"?call_id={call_id}"
    try:
        call = twilio_client.calls.create(
            to=to_number,
            from_=settings.twilio_from_number,
            url=twiml_url,
        )
    except TwilioRestException as e:
        # Surface Twilio's real reason (unverified number, trial limit, bad creds)
        # instead of a generic 500 — the caller can act on it.
        logger.warning(f"call rejected by Twilio code={e.code} status={e.status}: {e.msg}")
        return JSONResponse(
            status_code=e.status or 400,
            content={"error": "twilio_rejected", "code": e.code, "message": e.msg},
        )
    logger.info(f"call placed to={to_number} call_sid={call.sid} call_id={call_id}")
    return JSONResponse({"call_sid": call.sid, "status": call.status, "call_id": call_id})


@app.post("/dialout")
async def dialout(req: DialoutRequest):
    """Initiate an outbound call. Twilio dials `to_number` and streams to /ws."""
    return _place_call(req.to_number, req.call_id)


def _valid_e164(phone: str) -> bool:
    return phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone) <= 16


@app.post("/consent")
async def consent(req: ConsentRequest):
    """Script-tag endpoint: a shopper consented to a 'finish my cart' call.

    Stores the live cart, places the outbound call, and ties them via call_id so the
    agent talks about the REAL cart. Requires explicit consent + a valid E.164 phone.
    """
    if not req.consent:
        return JSONResponse(status_code=400, content={"error": "consent_required"})
    if not _valid_e164(req.phone):
        return JSONResponse(
            status_code=400, content={"error": "invalid_phone", "message": "expected E.164, e.g. +14155551234"}
        )

    call_id = uuid4().hex[:12]
    cart = Cart(
        call_id=call_id,
        phone=req.phone,
        store_name=req.store_name,
        customer_name=req.customer_name,
        currency=req.currency,
        items=[CartItem(title=i.title, qty=i.qty, price=i.price) for i in req.items],
        total=req.total,
        objection_hint=req.objection_hint,
    )
    _carts.put(cart)
    logger.info(
        f"consent store={req.store_id} call_id={call_id} phone={req.phone} "
        f"cart='{cart.summary()}' total={cart.total_str()}"
    )
    resp = _place_call(req.phone, call_id)
    return resp


@app.post("/twiml")
@app.get("/twiml")
async def twiml(request: Request):
    """TwiML that bridges the answered call into our media-stream WebSocket."""
    call_id = request.query_params.get("call_id", "")
    stream_url = settings.ws_url
    # Pass call_id to the WS via a <Parameter> so the bot can load per-call context.
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{stream_url}">'
        f'<Parameter name="call_id" value="{call_id}"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    return HTMLResponse(content=xml, media_type="application/xml")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Twilio media stream. Hands the socket to the pipecat bot."""
    await websocket.accept()
    transport_type, call_data = await parse_telephony_websocket(websocket)
    logger.info(f"ws connected transport={transport_type}")
    stream_sid = call_data["stream_id"]
    call_sid = call_data.get("call_id")
    # Our app-level call_id rides in the <Stream> custom <Parameter>, surfaced as `body`.
    app_call_id = (call_data.get("body") or {}).get("call_id")
    cart = _carts.get(app_call_id)
    logger.info(f"ws call_sid={call_sid} app_call_id={app_call_id} cart={'yes' if cart else 'none'}")

    async def _on_call_end(transcript: str) -> None:
        """Every real call is a labeled example: score it, learn if the agent whiffed."""
        if not cart or not transcript.strip():
            return
        objection = cart.objection_hint or "price"
        merchant = cart.store_name  # one merchant slot per store name for the demo
        report = await get_loop().learn_from_call(
            merchant, objection, transcript, store_name=cart.store_name
        )
        logger.info(
            f"learned from call_id={app_call_id}: {objection} "
            f"{'GREEN' if report.before.passed else 'RED→mined'}"
        )

    try:
        await run_bot(
            websocket,
            settings=settings,
            stream_sid=stream_sid,
            call_sid=call_sid,
            cart=cart,
            on_call_end=_on_call_end,
        )
    except Exception:
        logger.exception("bot run failed")
    finally:
        logger.info(f"ws closed call_sid={call_sid}")


# ── The Cekura self-improvement loop ─────────────────────────────────────────


class ImproveRequest(BaseModel):
    objection: str = "price"  # locked demo objection
    store_name: str = "the store"


def _result_json(r) -> dict:
    return {
        "objection": r.objection,
        "passed": r.passed,
        "score": round(r.score, 3),
        "reasoning": r.reasoning,
        "backend": r.backend,
        "strategy_version": r.strategy_version,
        "transcript": r.transcript,
    }


@app.post("/cekura/run/{merchant_id}")
async def cekura_run(merchant_id: str, req: ImproveRequest):
    """Run ONE eval against the agent's current prompt (no mining). Red or green."""
    result = await get_loop().evaluate_once(merchant_id, req.objection, req.store_name)
    return JSONResponse(_result_json(result))


@app.post("/improve/{merchant_id}")
async def improve(merchant_id: str, req: ImproveRequest):
    """The headline loop: run -> (if red) mine -> store -> re-run. Returns red->green."""
    report = await get_loop().improve(merchant_id, req.objection, req.store_name)
    return JSONResponse(
        {
            "objection": report.objection,
            "flipped_green": report.flipped_green,
            "before": _result_json(report.before),
            "after": _result_json(report.after) if report.after else None,
            "mined_exemplar": (
                {"objection": report.mined.objection, "corrected_handling": report.mined.corrected_handling}
                if report.mined
                else None
            ),
        }
    )


@app.post("/admin/reset/{merchant_id}")
async def admin_reset(merchant_id: str):
    """Clear learned state so the demo can run again from v0."""
    _store.reset(merchant_id)
    return JSONResponse({"ok": True, "merchant_id": merchant_id})


@app.get("/api/scoreboard/{merchant_id}")
async def scoreboard(merchant_id: str):
    """Per-objection run history + current strategy version → drives the demo scoreboard."""
    runs = _store.runs_for(merchant_id)
    return JSONResponse(
        {
            "merchant_id": merchant_id,
            "strategy_version": _store.strategy_version(merchant_id),
            "runs": [
                {
                    "objection": r["objection"],
                    "passed": r["passed"],
                    "score": round(r["score"], 3),
                    "strategy_version": r["strategy_version"],
                    "backend": r["backend"],
                }
                for r in runs
            ],
            "exemplars": [
                {"objection": e.objection, "corrected_handling": e.corrected_handling}
                for e in _store.exemplars_for(merchant_id)
            ],
        }
    )
