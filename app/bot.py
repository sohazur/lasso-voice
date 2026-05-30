"""The pipecat voice pipeline for a single Twilio call.

Flow: Twilio media-stream (8kHz mu-law) -> STT -> LLM context -> LLM -> TTS -> Twilio.
Built against pipecat 1.3.0 APIs (verified import paths). One bot instance per call;
`run_bot` is awaited by the /ws handler for the lifetime of the connection.
"""

from __future__ import annotations

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from .carts import Cart
from collections.abc import Awaitable, Callable

from .config import Settings, build_llm, build_stt, build_tts, llm_label, voice_label
from .prompts import GREETING, build_system_prompt


def _transcript_from_context(context: LLMContext) -> str:
    """Reconstruct an 'Agent:/Shopper:' transcript from the LLM context messages.

    The context IS the running record of the call — every assistant (agent) and user
    (shopper) turn is aggregated into it. We skip the system prompt.
    """
    lines: list[str] = []
    for m in context.get_messages():
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "assistant":
            lines.append(f"Agent: {content.strip()}")
        elif role == "user":
            lines.append(f"Shopper: {content.strip()}")
    return "\n".join(lines)


async def run_bot(
    websocket,
    *,
    settings: Settings,
    stream_sid: str,
    call_sid: str | None,
    cart: Cart | None = None,
    system_prompt: str | None = None,
    greeting: str | None = None,
    on_call_end: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Run one cart-recovery call to completion over an accepted Twilio WS."""
    logger.info(
        f"bot start call_sid={call_sid} stream_sid={stream_sid} "
        f"llm={llm_label(settings)} voice={voice_label(settings)}"
    )

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Twilio media streams are 8kHz mono mu-law; match to avoid resampling.
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )

    stt = build_stt(settings)
    tts = build_tts(settings)
    llm = build_llm(settings)

    # Condition the agent on the REAL abandoned cart when we have one (outbound path).
    if system_prompt is None and cart is not None:
        system_prompt = build_system_prompt(
            store_name=cart.store_name,
            customer_name=cart.customer_name,
            cart_summary=cart.summary(),
            cart_total=cart.total_str(),
        )
        logger.info(
            f"loaded cart for call_sid={call_sid}: {cart.summary()} ({cart.total_str()})"
        )
    if greeting is None and cart is not None:
        greeting = GREETING.format(
            customer_name=cart.customer_name, store_name=cart.store_name
        )

    context = LLMContext()
    context.set_messages(
        [{"role": "system", "content": system_prompt or build_system_prompt()}]
    )
    aggregators = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        # Speak first so the shopper hears the agent immediately on pickup.
        opener = greeting or "Hi, this is Lasso — you left a couple things in your cart, want a hand finishing up?"
        context.add_message({"role": "assistant", "content": opener})
        await task.queue_frames([aggregators.assistant().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        logger.info(f"client disconnected call_sid={call_sid}; cancelling task")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    # The call is over — hand the real transcript to the learning loop (Phase E).
    if on_call_end is not None:
        transcript = _transcript_from_context(context)
        try:
            await on_call_end(transcript)
        except Exception:
            logger.exception("on_call_end callback failed")
    logger.info(f"bot end call_sid={call_sid}")
