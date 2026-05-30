"""Environment + service factories for Lasso Voice.

The LLM factory is the one place that decides Nemotron-vs-OpenAI. Nemotron is the
model that "learns" in the Cekura loop, so we wire it first — but if its creds are
absent we fall back to OpenAI so a working call is never blocked by a sponsor
endpoint being down. Same OpenAI-compatible client either way (just a base_url swap).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    # LLM
    nemotron_base_url: str
    nemotron_api_key: str
    nemotron_model: str
    openai_api_key: str
    # STT / TTS
    deepgram_api_key: str
    cartesia_api_key: str
    cartesia_voice_id: str
    # Cekura (the learning signal — used in Phase 4)
    cekura_api_key: str
    # Supabase (Phase 3)
    supabase_url: str
    supabase_key: str

    @property
    def use_nemotron(self) -> bool:
        return bool(self.nemotron_base_url and self.nemotron_api_key)

    @property
    def ws_url(self) -> str:
        """wss:// URL Twilio's <Stream> connects back into."""
        base = self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base.rstrip('/')}/ws"


def load_settings() -> Settings:
    return Settings(
        public_base_url=_req("PUBLIC_BASE_URL"),
        twilio_account_sid=_req("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_req("TWILIO_AUTH_TOKEN"),
        twilio_from_number=_req("TWILIO_FROM_NUMBER"),
        nemotron_base_url=_opt("NEMOTRON_BASE_URL"),
        nemotron_api_key=_opt("NEMOTRON_API_KEY"),
        nemotron_model=_opt("NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b"),
        openai_api_key=_opt("OPENAI_API_KEY"),
        deepgram_api_key=_req("DEEPGRAM_API_KEY"),
        cartesia_api_key=_req("CARTESIA_API_KEY"),
        cartesia_voice_id=_opt("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        cekura_api_key=_opt("CEKURA_API_KEY"),
        supabase_url=_opt("SUPABASE_URL"),
        supabase_key=_opt("SUPABASE_KEY"),
    )


def build_llm(settings: Settings):
    """Nemotron via its OpenAI-compatible endpoint, else OpenAI GPT-4.1.

    Returns a pipecat OpenAILLMService either way — the only difference is base_url +
    api_key + model, which is exactly how pipecat intends provider swaps to work.
    """
    from pipecat.services.openai.llm import OpenAILLMService

    if settings.use_nemotron:
        return OpenAILLMService(
            base_url=settings.nemotron_base_url,
            api_key=settings.nemotron_api_key,
            settings=OpenAILLMService.Settings(model=settings.nemotron_model),
        )
    if not settings.openai_api_key:
        raise RuntimeError(
            "No LLM configured: set NEMOTRON_BASE_URL+NEMOTRON_API_KEY (preferred) "
            "or OPENAI_API_KEY (fallback)."
        )
    return OpenAILLMService(
        api_key=settings.openai_api_key,
        settings=OpenAILLMService.Settings(model="gpt-4.1"),
    )


def llm_label(settings: Settings) -> str:
    return f"nemotron:{settings.nemotron_model}" if settings.use_nemotron else "openai:gpt-4.1"
