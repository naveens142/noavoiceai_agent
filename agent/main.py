from __future__ import annotations
import os
import warnings
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional, List

warnings.filterwarnings("ignore", message=".*PyTorch was not found.*")

# IceServer is pipecat's own class (wraps aiortc internally)
from pipecat.transports.smallwebrtc.connection import IceServer

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
from agent.health import configure_health, patch_uvicorn_with_health

logger = get_logger(__name__)


# ─── ICE / TURN configuration ─────────────────────────────────────────────────

def _get_ice_servers() -> Optional[List[IceServer]]:
    """
    Returns IceServer list for SmallWebRTCConnection.
    Reads METERED_TURN_URL, METERED_TURN_USERNAME, METERED_TURN_CREDENTIAL
    directly from os.environ to avoid pydantic cache issues in Cloud Run.

    - Local dev: returns None → aiortc uses default STUN (P2P works fine)
    - Production: returns STUN + TURN from Metered.ca (required for Cloud Run NAT)
    - Twilio: this function is NOT used — Twilio handles its own media relay
    """
    turn_url   = os.environ.get("METERED_TURN_URL")
    username   = os.environ.get("METERED_TURN_USERNAME")
    credential = os.environ.get("METERED_TURN_CREDENTIAL")

    # Debug print — always visible in Cloud Run logs regardless of log config
    print(f"[TURN DEBUG] url={turn_url} username={username} credential_set={bool(credential)}", flush=True)

    if not turn_url or not username or not credential:
        print("[TURN DEBUG] Missing TURN config — using default STUN", flush=True)
        return None

    print(f"[TURN DEBUG] TURN configured: {turn_url}", flush=True)
    return [
        IceServer(urls="stun:stun.relay.metered.ca:80"),
        IceServer(urls=f"turn:{turn_url}:80",                 username=username, credential=credential),
        IceServer(urls=f"turn:{turn_url}:80?transport=tcp",   username=username, credential=credential),
        IceServer(urls=f"turn:{turn_url}:443",                username=username, credential=credential),
        IceServer(urls=f"turns:{turn_url}:443?transport=tcp", username=username, credential=credential),
    ]


# ─── Immutable app-level config ───────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    agent_name: str
    system_prompt: str
    first_message: str
    openai_model: str = field(default_factory=lambda: settings.openai_model)
    tts_voice: str = field(default_factory=lambda: settings.tts_voice)


_app_config: Optional[AppConfig] = None


# ─── Tool schema builder ──────────────────────────────────────────────────────

def _build_tools_schema(booking_tools: BookingTools) -> ToolsSchema:
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
            logger.error(
                "[%s] Failed to register tool '%s': %s", session_id, tool_name, exc, exc_info=True
            )
            raise


# ─── Core pipeline builder ────────────────────────────────────────────────────
#
# Transport-agnostic. Works with SmallWebRTCTransport today, TwilioTransport later.
# Only the transport object changes — LLM, STT, TTS, tools, context stay identical.
#
async def _build_pipeline(
    transport,
    session_id: str,
    booking_tools: Optional[BookingTools],
) -> tuple[PipelineTask, LLMContext]:

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

    if booking_tools:
        _register_tool_handlers(llm, booking_tools, session_id)
    else:
        logger.warning("[%s] No tools — conversation-only mode", session_id)

    context_messages = [{"role": "system", "content": _app_config.system_prompt}]

    if booking_tools:
        tools_schema = _build_tools_schema(booking_tools)
        context = LLMContext(messages=context_messages, tools=tools_schema)
        logger.info("[%s] LLMContext: %d tools", session_id, len(tools_schema.standard_tools))
    else:
        context = LLMContext(messages=context_messages)
        logger.warning("[%s] LLMContext has NO tools", session_id)

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    return task, context


# ─── bot() — WebRTC entry point (browser UI calls) ───────────────────────────

async def bot(runner_args: SmallWebRTCRunnerArguments) -> None:
    assert _app_config is not None, "bot() called before create_app()"

    session_id = str(uuid.uuid4())[:8]
    logger.info("[%s] ═══ SESSION START (WebRTC) ═══", session_id)

    api_client = APIClient()
    booking_tools: Optional[BookingTools] = None

    try:
        await api_client.open()
        await api_client.login()
        booking_tools = BookingTools(api_client)
        logger.info("[%s] API client ready", session_id)
    except Exception as exc:
        logger.error("[%s] API client init failed: %s — no tools", session_id, exc)

    # ── Inject ICE/TURN onto the connection object ────────────────────────────
    # SmallWebRTCConnection accepts ice_servers directly (pipecat 0.0.104).
    # The connection is created by the runner — we inject TURN config here
    # before handing it to the transport.
    ice = _get_ice_servers()
    print(f"[ICE DEBUG] Setting ice_servers on connection: {ice}", flush=True)
    runner_args.webrtc_connection.ice_servers = ice

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    task, context = await _build_pipeline(transport, session_id, booking_tools)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[%s] Client connected", session_id)
        messages = context.get_messages()
        messages.append({
            "role": "system",
            "content": "Please say hello and briefly introduce yourself. Keep it friendly and concise.",
        })
        context.set_messages(messages)
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[%s] Client disconnected", session_id)
        await task.cancel()
        context.set_messages([{"role": "system", "content": _app_config.system_prompt}])

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        logger.info("[%s] Releasing API client", session_id)
        try:
            await asyncio.wait_for(api_client.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] API client close timed out", session_id)
        except Exception as exc:
            logger.warning("[%s] API client close error: %s", session_id, exc)
        logger.info("[%s] ═══ SESSION END ═══", session_id)


# ─── twilio_bot() — uncomment when adding Twilio ─────────────────────────────
#
# async def twilio_bot(websocket) -> None:
#     from pipecat.transports.services.twilio import TwilioTransport
#     session_id = str(uuid.uuid4())[:8]
#     logger.info("[%s] ═══ SESSION START (Twilio) ═══", session_id)
#     api_client = APIClient()
#     booking_tools = None
#     try:
#         await api_client.open()
#         await api_client.login()
#         booking_tools = BookingTools(api_client)
#     except Exception as exc:
#         logger.error("[%s] API client init failed: %s", session_id, exc)
#     transport = TwilioTransport(
#         websocket=websocket,
#         params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
#     )
#     task, context = await _build_pipeline(transport, session_id, booking_tools)
#     @transport.event_handler("on_client_connected")
#     async def on_connected(transport, client):
#         messages = context.get_messages()
#         messages.append({"role": "system", "content": "Say hello briefly."})
#         context.set_messages(messages)
#         await task.queue_frames([LLMRunFrame()])
#     runner = PipelineRunner(handle_sigint=False)
#     try:
#         await runner.run(task)
#     finally:
#         await api_client.close()


# ─── create_app() ─────────────────────────────────────────────────────────────

async def create_app() -> None:
    global _app_config

    validate_settings()

    logger.info("═" * 60)
    logger.info("PIPECAT AGENT STARTUP")
    logger.info("═" * 60)

    startup_client = APIClient()
    agent_name = settings.agent_name
    system_prompt = get_system_prompt("default")
    first_message = "Hello! Thank you for calling. How can I help you today?"
    tool_count = 0

    try:
        await startup_client.open()
        await startup_client.login()
        logger.info("Startup auth OK")

        config_data = await startup_client.get_agent_config()
        agent_name = config_data.get("name", agent_name)
        system_prompt = config_data.get("system_prompt", system_prompt)
        first_message = config_data.get("first_message", first_message)
        logger.info("Agent config loaded: %s", agent_name)

        validation_tools = BookingTools(startup_client)
        test_schema = _build_tools_schema(validation_tools)
        tool_count = len(test_schema.standard_tools)
        logger.info("Tool schema OK: %d tools", tool_count)

    except Exception as exc:
        logger.warning("Startup API call failed (%s) — using defaults", exc)
    finally:
        try:
            await asyncio.wait_for(startup_client.close(), timeout=5.0)
        except Exception:
            pass

    if tool_count == 0:
        logger.warning(
            "Tool schema returned 0 tools — backend may be unreachable. "
            "Agent will start in conversation-only mode."
        )

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
    logger.info("READY")
    logger.info("═" * 60)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    asyncio.run(create_app())
    configure_health(_app_config)
    patch_uvicorn_with_health()
    port = os.getenv("PORT", "7860")
    sys.argv += ["--transport", "webrtc", "--host", "0.0.0.0", "--port", port]
    pipecat_main()