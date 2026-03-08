# agent/main.py
"""
Pipecat voice agent — production-grade, Pipecat Cloud ready.

Design decisions:
  - AppConfig is immutable after startup (frozen dataclass) — safe to share
  - Each bot() session owns its own APIClient + BookingTools — no shared state
  - login() called once per session to get JWT for booking endpoints
  - AGENT_API_TOKEN is Pipecat Cloud key — unused locally, used on cloud deploy
  - Graceful shutdown with timeout on API client close
  - Session-scoped logging with short UUID for log correlation
  - Tool registration failures raise immediately (fail loudly)
  - JWT expiry mid-session handled via auto re-login in APIClient
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

# Context
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

# Schemas
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# Services
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

# VAD
from pipecat.audio.vad.silero import SileroVADAnalyzer

# Transport
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams

# Runner
from pipecat.runner.utils import SmallWebRTCRunnerArguments

# Frames
from pipecat.frames.frames import LLMRunFrame

# LLM function call support
from pipecat.services.llm_service import FunctionCallParams

# Pipecat runner entrypoint
from pipecat.runner.run import main as pipecat_main

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.prompts import get_system_prompt
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)


# ─── Immutable app-level config ───────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    """
    Loaded once at startup from your FastAPI backend.
    Immutable after creation — safe to share across all concurrent sessions.
    Never holds per-session state (tokens, clients, contexts).
    """
    agent_name: str
    system_prompt: str
    first_message: str
    openai_model: str = field(default_factory=lambda: settings.openai_model)
    tts_voice: str = field(default_factory=lambda: settings.tts_voice)


# Single shared instance — set by create_app(), never mutated after
_app_config: Optional[AppConfig] = None


# ─── Tool schema builder ──────────────────────────────────────────────────────

def _build_tools_schema(booking_tools: BookingTools) -> ToolsSchema:
    """
    Convert raw OpenAI-format tool dicts → ToolsSchema.

    ToolsSchema only accepts FunctionSchema objects.
    Raw dicts are silently rejected, leaving the LLM with no tools.
    """
    raw_tools = booking_tools.get_tools_definition()
    schemas = []
    for tool in raw_tools:
        fn = tool["function"]
        params = fn.get("parameters", {})
        schemas.append(
            FunctionSchema(
                name=fn["name"],
                description=fn.get("description", ""),
                properties=params.get("properties", {}),
                required=params.get("required", []),
            )
        )
    names = [s.name for s in schemas]
    logger.debug("ToolsSchema built: %d tools — %s", len(schemas), names)
    return ToolsSchema(standard_tools=schemas)


# ─── Tool handler registration ────────────────────────────────────────────────

def _register_tool_handlers(
    llm: OpenAILLMService,
    booking_tools: BookingTools,
    session_id: str,
) -> None:
    """
    Register per-session tool handlers on the LLM service.

    CRITICAL: Each handler must have EXACTLY 1 parameter (FunctionCallParams).
    Pipecat inspects the signature to detect old vs new calling convention.
    Any extra params (even with defaults) triggers the legacy 6-arg path → crash.
    Use make_handler() factory to capture `name` via closure.
    """
    # Read-only tools are safe to cancel on user interruption.
    # Write tools (book, reschedule, cancel) must complete even if user speaks.
    read_only_tools = {"get_available_slots", "get_booking"}

    tool_names = [
        schema["function"]["name"]
        for schema in booking_tools.get_tools_definition()
    ]

    logger.info("[%s] Registering %d tool handlers: %s", session_id, len(tool_names), tool_names)

    for tool_name in tool_names:

        def make_handler(name: str):
            async def _handler(params: FunctionCallParams):
                logger.info("[%s] Tool called: %s | args=%s", session_id, name, dict(params.arguments))
                try:
                    result = await booking_tools.handle_tool_call(name, dict(params.arguments))
                except Exception as exc:
                    logger.error(
                        "[%s] Tool exception: %s — %s", session_id, name, exc, exc_info=True
                    )
                    await params.result_callback({
                        "error": "Internal tool error",
                        "code": "EXCEPTION",
                    })
                    return

                if result.get("status") == "success":
                    logger.info("[%s] Tool success: %s", session_id, name)
                    await params.result_callback(result["data"])
                else:
                    logger.warning(
                        "[%s] Tool error: %s — %s", session_id, name, result.get("message")
                    )
                    await params.result_callback({
                        "error": result.get("message", "Unknown error"),
                        "code": result.get("error"),
                    })
            return _handler

        try:
            llm.register_function(
                tool_name,
                make_handler(tool_name),
                cancel_on_interruption=(tool_name in read_only_tools),
            )
            logger.debug("[%s] Tool registered: %s", session_id, tool_name)
        except Exception as exc:
            # Fail loudly — a broken tool schema produces a silently toolless agent
            logger.error(
                "[%s] Failed to register tool '%s': %s", session_id, tool_name, exc, exc_info=True
            )
            raise


# ─── bot() — called per WebRTC connection ────────────────────────────────────

async def bot(runner_args: SmallWebRTCRunnerArguments) -> None:
    """
    Entry point called by the Pipecat runner for each new WebRTC connection.

    Stateless with respect to other sessions — every resource is created
    fresh and destroyed when the call ends. The only shared object is
    _app_config which is intentionally frozen/immutable.

    Auth flow:
      - login() called once per session → short-lived JWT stored on api_client
      - JWT used for all booking endpoint calls during this session
      - If JWT expires mid-call, APIClient auto re-authenticates (see api_client.py)
      - AGENT_API_TOKEN (Pipecat Cloud key) is not used here — only on cloud deploy
    """
    assert _app_config is not None, (
        "bot() called before create_app() completed. Check startup order in __main__."
    )

    session_id = str(uuid.uuid4())[:8]
    logger.info("[%s] ═══════════════ SESSION STARTING ═══════════════", session_id)

    # ── Per-session API client ────────────────────────────────────────────────
    # Owns its own httpx connection pool and JWT — fully isolated from other sessions.
    api_client = APIClient()
    booking_tools: Optional[BookingTools] = None

    try:
        await api_client.open()
        await api_client.login()   # POST /auth/login → JWT stored on client
        booking_tools = BookingTools(api_client)
        logger.info("[%s] API client ready, JWT acquired", session_id)
    except Exception as exc:
        # Degrade gracefully — agent still works for conversation, just no booking
        logger.error(
            "[%s] API client init failed: %s — proceeding without tools", session_id, exc
        )

    # ── Transport ─────────────────────────────────────────────────────────────
    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # ── Services ──────────────────────────────────────────────────────────────
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        model="nova-2",
        language="en",
        smart_format=True,
        interim_results=True,
    )

    llm = OpenAILLMService(
        api_key=settings.openai_api_key,
        model=_app_config.openai_model,
    )

    tts = OpenAITTSService(
        api_key=settings.openai_api_key,
        voice=_app_config.tts_voice,
    )

    # ── Tool registration ─────────────────────────────────────────────────────
    if booking_tools:
        _register_tool_handlers(llm, booking_tools, session_id)
    else:
        logger.warning("[%s] No tools registered — agent running in conversation-only mode", session_id)

    # ── LLM context ───────────────────────────────────────────────────────────
    context_messages = [{"role": "system", "content": _app_config.system_prompt}]

    if booking_tools:
        tools_schema = _build_tools_schema(booking_tools)
        context = LLMContext(messages=context_messages, tools=tools_schema)
        logger.info(
            "[%s] LLMContext ready with %d tools", session_id, len(tools_schema.standard_tools)
        )
    else:
        context = LLMContext(messages=context_messages)
        logger.warning("[%s] LLMContext has NO tools", session_id)

    # ── Aggregators ───────────────────────────────────────────────────────────
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    # ── Pipeline ──────────────────────────────────────────────────────────────
    pipeline = Pipeline([
        transport.input(),       # Audio in (VAD gated)
        stt,                     # Speech → Text
        user_aggregator,         # User utterance → context
        llm,                     # LLM inference + tool dispatch
        tts,                     # Text → Speech
        transport.output(),      # Audio out
        assistant_aggregator,    # Assistant + tool results → context
    ])

    # ── Task ──────────────────────────────────────────────────────────────────
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # ── Event handlers ────────────────────────────────────────────────────────

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[%s] Client connected", session_id)
        # One-shot greeting — append to context then trigger LLM immediately.
        # This extra system message is intentional: it produces a warm opening
        # without hard-coding a greeting string in the main system prompt.
        messages = context.get_messages()
        messages.append({
            "role": "system",
            "content": (
                "Please say hello and briefly introduce yourself. "
                "Keep it friendly and concise."
            ),
        })
        context.set_messages(messages)
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[%s] Client disconnected — cancelling pipeline", session_id)
        await task.cancel()
        # Reset context so a same-process reconnect starts clean
        context.set_messages([{"role": "system", "content": _app_config.system_prompt}])

    # ── Run ───────────────────────────────────────────────────────────────────
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        # Always close the per-session API client regardless of how session ends:
        # normal hangup, client disconnect, exception, or pipeline cancellation.
        logger.info("[%s] Session ending — releasing API client", session_id)
        try:
            await asyncio.wait_for(api_client.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] API client close timed out (5s)", session_id)
        except Exception as exc:
            logger.warning("[%s] API client close error: %s", session_id, exc)
        logger.info("[%s] ═══════════════ SESSION ENDED ═══════════════", session_id)


# ─── create_app() — one-time startup ─────────────────────────────────────────

async def create_app() -> None:
    """
    One-time startup routine. Must complete before pipecat_main().

    Responsibilities:
      1. Validate all required settings are present
      2. Fetch agent config from FastAPI backend (name, system_prompt, etc.)
      3. Validate tool schema — fail loudly at startup, not silently at call time
      4. Build immutable AppConfig shared by all sessions

    Uses a short-lived client that is closed after startup.
    Per-session clients are created inside bot().

    NOTE: AGENT_API_TOKEN (Pipecat Cloud key) is not used here.
          It will be used when switching to DailyTransport for cloud deployment.
    """
    global _app_config

    validate_settings()

    logger.info("═" * 60)
    logger.info("PIPECAT AGENT STARTUP")
    logger.info("═" * 60)

    # Short-lived startup client — only used to fetch config and validate tools
    startup_client = APIClient()
    agent_name = settings.agent_name
    system_prompt = get_system_prompt("default")
    first_message = "Hello! Thank you for calling. How can I help you today?"
    tool_count = 0

    try:
        await startup_client.open()
        await startup_client.login()
        logger.info("Startup auth OK")

        # Fetch agent config
        config_data = await startup_client.get_agent_config()
        agent_name = config_data.get("name", agent_name)
        system_prompt = config_data.get("system_prompt", system_prompt)
        first_message = config_data.get("first_message", first_message)
        logger.info("Agent config loaded: %s", agent_name)

        # Validate tool schema using the same startup client
        # Reusing the client avoids a second login() round-trip
        validation_tools = BookingTools(startup_client)
        test_schema = _build_tools_schema(validation_tools)
        tool_count = len(test_schema.standard_tools)
        logger.info("Tool schema OK: %d tools", tool_count)

    except Exception as exc:
        logger.warning("Startup API call failed (%s) — using defaults", exc)
        # Don't raise here — agent can still run with default config
        # Tool schema failure is the only hard stop (see below)
    finally:
        try:
            await asyncio.wait_for(startup_client.close(), timeout=5.0)
        except Exception:
            pass

    # Hard stop if tool schema is broken — better to crash at startup than
    # to silently serve callers who can't book anything
    if tool_count == 0:
        raise RuntimeError(
            "Tool schema validation returned 0 tools. "
            "Check BookingTools.get_tools_definition() and your API connection."
        )

    # Freeze config — immutable from this point forward
    _app_config = AppConfig(
        agent_name=agent_name,
        system_prompt=system_prompt,
        first_message=first_message,
    )

    logger.info("Agent : %s", _app_config.agent_name)
    logger.info("Model : %s", _app_config.openai_model)
    logger.info("Voice : %s", _app_config.tts_voice)
    logger.info("Tools : %d", tool_count)
    logger.info("═" * 60)
    logger.info("READY — http://localhost:7860/client")
    logger.info("═" * 60)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    # Order is critical:
    #   1. create_app() — auth, config fetch, tool validation, AppConfig freeze
    #   2. pipecat_main() — starts uvicorn + WebRTC server, calls bot() per connection
    #
    # AGENT_API_TOKEN in .env is your Pipecat Cloud key — unused locally.
    # It will authenticate your agent with Pipecat Cloud on cloud deployment.
    asyncio.run(create_app())
    pipecat_main()