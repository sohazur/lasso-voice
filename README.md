# Lasso Voice

A self-improving cart-recovery voice agent. Drop a `<script>` tag on any store; when a shopper
abandons checkout, an exit modal offers a **consented "Call me to finish" button**. Tap it and an
AI voice agent (pipecat + Twilio) calls to recover the cart by handling the shopper's objection.

The agent **improves itself via a Cekura eval loop**: Cekura simulates abandoning shoppers throwing
real objections (too expensive, shipping too slow, just browsing) and scores the agent. When the
agent whiffs an objection (red), the harness mines that failure into a corrected exemplar fed back
to the model. Same objection later → handled clean (green). No human in the loop. The eval *is* the
environment, so the agent learns with zero real buyers.

## The 60-second story
- **9am** — shopper bails, agent jumps in, fumbles the *price* objection → Cekura flashes **red**.
- *(seconds later)* — the eval catches it, the loop folds the fix into the model. No human touches it.
- **2pm** — same price objection → agent nails it → score flips **green**.
- **Scoreboard climbs across the day** → *"your site just saved a sale at 2am."*

## Stack
- **pipecat** — STT → LLM → TTS voice pipeline
- **Twilio** — telephony (media streams)
- **NVIDIA Nemotron** — the LLM that learns (OpenAI fallback)
- **Cekura** — eval-to-improve; the learning signal
- **Supabase** — calls, eval runs, mined exemplars
- **FastAPI** — control plane

## Quickstart
```bash
uv sync
cp .env.example .env   # fill in keys
uv run uvicorn server:app --reload --port 7860
ngrok http 7860        # set PUBLIC_BASE_URL to the https URL
```

## Next (post-hackathon)
- Post-call **SMS** with cart deep-link (deferred from v1).
- **Cold consented outbound** (call shoppers back later, with prior consent) — TCPA-safe path.
- More objections beyond price: shipping cost, "just browsing", trust/returns.

---
*From-scratch rebuild of the original Lasso (AgentPhone-based). This version is pipecat + Twilio + Cekura.*
