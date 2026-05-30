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
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from .config import Settings, build_llm, llm_label
from .prompts import build_system_prompt


async def run_bot(
    websocket,
    *,
    settings: Settings,
    stream_sid: str,
    call_sid: str | None,
    system_prompt: str | None = None,
    greeting: str | None = None,
) -> None:
    """Run one cart-recovery call to completion over an accepted Twilio WS."""
    logger.info(f"bot start call_sid={call_sid} stream_sid={stream_sid} llm={llm_label(settings)}")

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

    stt = DeepgramSTTService(api_key=settings.deepgram_api_key)
    tts = CartesiaTTSService(
        api_key=settings.cartesia_api_key,
        voice_id=settings.cartesia_voice_id,
    )
    llm = build_llm(settings)

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
    logger.info(f"bot end call_sid={call_sid}")
