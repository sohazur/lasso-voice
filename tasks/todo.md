# Lasso Voice — Product Build Plan

Goal: turn the two proven cores (outbound call + self-improvement loop) into a
demoable product: merchant dashboard → script tag → test store → real call.

## Decisions (locked with user 2026-05-30)
- Dashboard: **server-rendered HTML inside FastAPI** (no separate frontend / build step).
- Demo flow: **real outbound call to a test phone** (script tag → /consent → /dialout).
- Test store: **a fake store page I build** (self-contained /demo-store).
- Cart data: **captured by the script tag** at abandonment, POSTed to /consent.
- STT/TTS: Gradium stays (NVIDIA ws:// ASR parked). LLM: Nemotron (live).

## Current state (verified)
- [x] Outbound call pipeline — PROVEN (1 call connected, 13s, charged).
- [x] Self-improvement loop — PROVEN live (Nemotron red→green via /improve).
- [x] /dialout now surfaces real Twilio errors (not generic 500).
- [x] dashboard.html exists (191 lines, polished red→green scoreboard)…
      …but is NOT served by any route, and calls /admin/reset/{m} which doesn't exist.
- [ ] No script tag, no /consent, no exit-intent modal, no test store, no cart context.
- ⚠️ Twilio fraud-hold (21216) blocking repeat calls — ops issue, not code. Clears with
      time / support ticket. Does not block building the product layer.

## Plan (phased — each phase independently testable)

### Phase A — Wire up what already exists (small, unblocks the dashboard)
- [x] A1. Serve dashboard.html via `GET /` (FileResponse). DONE (pre-existing).
- [x] A2. `POST /admin/reset/{merchant_id}` + LoopStore.reset(). DONE (pre-existing).
- [ ] A3. Add a placeholder-config flag to /health (warn if PUBLIC_BASE_URL still the example).
- [x] Verify: GET / → 200 HTML; reset → v0; scoreboard empty. CONFIRMED live.

### Phase B — Cart-context seam (makes the call about a REAL cart)  ✅ DONE
- [x] B1. app/carts.py: Cart/CartItem/CartStore (in-memory, keyed by call_id).
- [x] B2. bot.run_bot accepts `cart`; builds system prompt + greeting from real cart.
- [x] B3. /ws reads app call_id from call_data["body"] (the <Parameter>), loads cart, passes it.
- [x] Verify: offline test confirms cart threads into prompt + greeting (Acme/Ethiopia/$120.00).

### Phase C — Consent endpoint + script tag (the embeddable product)  ✅ DONE (C3 pending)
- [x] C1. POST /consent: validates consent flag + E.164 phone, stores cart under a uuid
        call_id, places the call via shared _place_call(); surfaces Twilio errors (not 500).
- [x] C2. GET /embed.js: exit-intent modal, reads window.LASSO_CART, POSTs to /consent
        on the script's own origin. Consent fine-print included.
- [ ] C3. Snippet generator on the dashboard (copy-paste <script…> for the merchant). PENDING.
- [x] Verify (live): /consent guards (400 consent_required / invalid_phone) work; valid
        consent stores cart + reaches Twilio (rejected only by the 21216 fraud hold).

### Phase D — Fake test store (where the tag lives)  ✅ DONE
- [x] D1. GET /demo-store: storefront sets window.LASSO_CART + loads /embed.js.
- [x] D2. "Leave checkout" button + real exit-intent (mouseout top) trigger the modal.
- [ ] Verify (full, blocked on Twilio): open /demo-store → abandon → modal → phone →
        real call rings → agent talks about the real cart. BLOCKED ONLY by 21216 hold.

### Phase C3 — Dashboard snippet generator  ✅ DONE
- [x] C3. /api/config returns the live snippet; dashboard shows a copy-paste box + button.

### Phase E — Learn from every REAL outbound call  ✅ DONE
Design: LLMContext IS the running transcript. On call end, reconstruct via
context.get_messages(), score it, mine a fix if it failed.
- [x] E1. bot._transcript_from_context() rebuilds Agent/Shopper transcript from context;
        run_bot gained on_call_end callback fired after the call completes.
- [x] E2. ImprovementLoop.learn_from_call(): scores the REAL transcript (LocalJudge
        .score_transcript), mines + stores an exemplar if RED. /ws wires on_call_end to it.
- [x] E3. Live runs recorded with backend="live-call" → show on the scoreboard.
- [x] Verified: tests/test_live_learning.py (13 assertions) passes offline.

### Phase F — Cekura (real scoring), drop-in
FINDINGS (probed 2026-05-30): key is VALID; MCP handshake works; 145 tools available.
  - The old code called `observe_create` — THAT TOOL DOESN'T EXIST -> 401. Wrong tool.
  - Correct tool: `scenarios_run_text` (text/websocket sim; cheap). Needs agent_id +
    evaluator/scenario IDs, created in the Cekura dashboard.
  - projects_list also 401s because NO AGENT EXISTS yet in the project. Once the user
    creates an agent + a basic evalset, tool calls authorize.
USER ACTION (in Cekura dashboard, "Create an Agent"):
  - Provider: Custom. External Assistant ID: `lasso-cart-agent` (-> CEKURA_ASSISTANT_ID).
  - Turn ON "Send Post Conversation Metadata" (lets us POST real transcripts later).
  - Connections: DESELECT Telephony, IGNORE SIP entirely, select CHAT > Websocket.
  - Create. Then create ONE price-objection evaluator. Give me agent_id + scenario IDs.
- [x] F1. Rewrote CekuraEvaluator to call `scenarios_run_text` (agent_id/assistant_id +
        scenario ids); replaced dead observe_create + _score_from_run. Raises (→ local-judge
        fallback) until CEKURA_AGENT_ID + CEKURA_SCENARIO_IDS are set.
- [x] F2. Added CEKURA_AGENT_ID + CEKURA_SCENARIO_IDS to config + .env.example (documented
        the exact dashboard steps). PENDING USER: create agent + evalset, fill the two vars.

## Review (2026-05-30)
Built the full product layer on top of the two proven cores. Verified:
- /, /demo-store, /embed.js, /api/config, /consent, /health all 200 and correct.
- /consent guards (consent + E.164) work; valid consent stores cart + reaches Twilio.
- Cart threads into the call's system prompt + greeting (real items/total).
- LIVE-CALL LEARNING: every completed call's transcript is scored; a whiff mines a fix
  that conditions the next call. tests/test_live_learning.py (13 assertions) green.
- Cekura path corrected to the real tool; falls back to local-judge honestly until the
  user creates the agent/evalset and sets CEKURA_AGENT_ID + CEKURA_SCENARIO_IDS.
- Loop regression (tests/test_loop.py) still green; ruff clean across app/ + tests/.

BLOCKED (ops, not code): live phone ring needs Twilio 21216 fraud-hold cleared.
PENDING USER: (1) Cekura agent + evalset → 2 env vars; (2) Twilio support ticket.
NEXT (optional): /call-status webhook for live Twilio status per attempt.

## Out of scope (for now)
- Shopify theme integration (document how to drop the same tag later).
- Auth / multi-tenant accounts (single hardcoded merchant "acme" for the demo).
- SMS follow-up, persistence to Supabase (in-memory is fine for the demo).

## Review (filled in as phases complete)
- (pending)
