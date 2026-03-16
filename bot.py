"""
bot.py
Pipecat Voice Agent - Bot Module
Uses SmallWebRTC transport with RTVI for transcript delivery over data channel.
"""

from __future__ import annotations
import os
import json
import warnings
import asyncio
from dataclasses import dataclass, field
from typing import Optional

warnings.filterwarnings("ignore", message=".*PyTorch was not found.*")

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams

# RTVI: RTVIProcessor is prepended by PipelineTask so data channel frames route
# correctly through transport.output(). RTVIObserver is also added by PipelineTask.
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIObserverParams

from pipecat.frames.frames import LLMRunFrame
from pipecat.services.llm_service import FunctionCallParams

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.prompts import get_system_prompt
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)


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
    return ToolsSchema(standard_tools=schemas)



# ─── Tool handler registration ────────────────────────────────────────────────

def _register_tool_handlers(llm, booking_tools, session_id):
    read_only_tools = {"get_available_slots", "get_booking"}
    tool_names = [s["function"]["name"] for s in booking_tools.get_tools_definition()]
    logger.info("[%s] Registering %d tools", session_id, len(tool_names))

    for tool_name in tool_names:
        def make_handler(name):
            async def _handler(params: FunctionCallParams):
                logger.info("[%s] Tool: %s args=%s", session_id, name, dict(params.arguments))
                try:
                    result = await booking_tools.handle_tool_call(name, dict(params.arguments))
                except Exception as exc:
                    logger.error("[%s] Tool exception %s: %s", session_id, name, exc)
                    await params.result_callback({"error": "Internal error", "code": "EXCEPTION"})
                    return
                if result.get("status") == "success":
                    await params.result_callback(result["data"])
                else:
                    await params.result_callback({
                        "error": result.get("message", "Unknown error"),
                        "code": result.get("error"),
                    })
            return _handler

        llm.register_function(
            tool_name,
            make_handler(tool_name),
            cancel_on_interruption=(tool_name in read_only_tools),
        )


# ─── Pipeline builder ─────────────────────────────────────────────────────────

async def _build_pipeline(
    transport: SmallWebRTCTransport,
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
        model="tts-1",
        voice=_app_config.tts_voice,
    )

    # Use standard RTVIProcessor for data channel management and RTVI protocol.
    # IMPORTANT: Do NOT add rtvi to the Pipeline list.
    # PipelineTask will prepend it BEFORE the pipeline so its
    # OutputTransportMessageUrgentFrame flows downstream to transport.output().
    # Placing rtvi at the end of the Pipeline means _next=None and all data
    # channel messages are silently dropped.
    rtvi = RTVIProcessor(transport=transport)

    if booking_tools:
        _register_tool_handlers(llm, booking_tools, session_id)

    context_messages = [{"role": "system", "content": _app_config.system_prompt}]

    if booking_tools:
        tools_schema = _build_tools_schema(booking_tools)
        context = LLMContext(messages=context_messages, tools=tools_schema)
        logger.info("[%s] Tools: %d", session_id, len(tools_schema.standard_tools))
    else:
        context = LLMContext(messages=context_messages)

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
        # rtvi is NOT here — PipelineTask prepends it before this pipeline
    ])

    # PipelineTask prepends RTVIProcessor and adds RTVIObserver automatically.
    # We pass our pre-built rtvi (with transport wired) and observer params.
    task = PipelineTask(
        pipeline,
        rtvi_processor=rtvi,
        rtvi_observer_params=RTVIObserverParams(
            user_transcription_enabled=True,   # user speech text → client
            bot_tts_enabled=True,              # bot TTS text chunks → client
            bot_speaking_enabled=True,         # bot speaking start/stop events
            user_speaking_enabled=True,        # user speaking start/stop events
            bot_output_enabled=False,          # skip aggregated output (using tts chunks)
            bot_llm_enabled=False,             # skip raw LLM token stream
            metrics_enabled=False,
        ),
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    return task, context


# ─── bot() ────────────────────────────────────────────────────────────────────

async def bot(transport: SmallWebRTCTransport, session_id: str) -> None:
    assert _app_config is not None, "bot() called before initialize_app()"
    logger.info("[%s] ═══ SESSION START ═══", session_id)

    api_client = APIClient()
    booking_tools: Optional[BookingTools] = None

    try:
        await api_client.open()
        await api_client.login()
        booking_tools = BookingTools(api_client)
    except Exception as exc:
        logger.error("[%s] API init failed: %s", session_id, exc)

    task, context = await _build_pipeline(transport, session_id, booking_tools)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, webrtc_connection):
        logger.info("[%s] ✅ Client connected", session_id)
        messages = context.get_messages()
        messages.append({
            "role": "system",
            "content": "Please say hello and briefly introduce yourself. Keep it friendly and concise.",
        })
        context.set_messages(messages)
        await task.queue_frames([LLMRunFrame()])
    
    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, webrtc_connection):
        logger.info("[%s] Client disconnected", session_id)
        await task.cancel()
        context.set_messages([{"role": "system", "content": _app_config.system_prompt}])

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        try:
            await asyncio.wait_for(api_client.close(), timeout=5.0)
        except Exception:
            pass
        logger.info("[%s] ═══ SESSION END ═══", session_id)


# ─── initialize_app() ─────────────────────────────────────────────────────────

async def initialize_app() -> None:
    global _app_config
    validate_settings()

    logger.info("═" * 60)
    logger.info("PIPECAT AGENT STARTUP (SmallWebRTC + RTVI Transcripts)")
    logger.info("═" * 60)

    startup_client = APIClient()
    agent_name    = settings.agent_name
    system_prompt = get_system_prompt("default")
    first_message = "Hello! Thank you for calling. How can I help you today?"
    tool_count    = 0

    try:
        await startup_client.open()
        await startup_client.login()
        config_data   = await startup_client.get_agent_config()
        agent_name    = config_data.get("name", agent_name)
        system_prompt = config_data.get("system_prompt", system_prompt)
        first_message = config_data.get("first_message", first_message)
        validation_tools = BookingTools(startup_client)
        tool_count = len(_build_tools_schema(validation_tools).standard_tools)
        logger.info("Startup OK — %d tools", tool_count)
    except Exception as exc:
        logger.warning("Startup API failed (%s) — using defaults", exc)
    finally:
        try:
            await asyncio.wait_for(startup_client.close(), timeout=5.0)
        except Exception:
            pass

    _app_config = AppConfig(
        agent_name=agent_name,
        system_prompt=system_prompt,
        first_message=first_message,
    )

    logger.info("Agent : %s | Model : %s | Voice : %s | Tools : %d",
                _app_config.agent_name, _app_config.openai_model,
                _app_config.tts_voice, tool_count)
    logger.info("READY")
    logger.info("═" * 60)