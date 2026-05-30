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

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel
from pipecat.runner.utils import parse_telephony_websocket
from twilio.rest import Client as TwilioClient

from .bot import run_bot
from .config import llm_label, load_settings
from .loop.improve import ImprovementLoop
from .loop.store import LoopStore

settings = load_settings()
twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

app = FastAPI(title="Lasso Voice")

# One shared store so /improve, /cekura/run and /api/scoreboard see the same state.
_store = LoopStore(settings)
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


@app.get("/health")
async def health():
    return {
        "ok": True,
        "llm": llm_label(settings),
        "from_number": settings.twilio_from_number,
        "ws_url": settings.ws_url,
        "cekura_configured": bool(settings.cekura_api_key),
    }


@app.post("/dialout")
async def dialout(req: DialoutRequest):
    """Initiate an outbound call. Twilio dials `to_number` and streams to /ws."""
    twiml_url = f"{settings.public_base_url.rstrip('/')}/twiml"
    if req.call_id:
        twiml_url += f"?call_id={req.call_id}"
    call = twilio_client.calls.create(
        to=req.to_number,
        from_=settings.twilio_from_number,
        url=twiml_url,
    )
    logger.info(f"dialout to={req.to_number} call_sid={call.sid} call_id={req.call_id}")
    return JSONResponse({"call_sid": call.sid, "status": call.status})


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
    try:
        await run_bot(
            websocket,
            settings=settings,
            stream_sid=stream_sid,
            call_sid=call_sid,
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
