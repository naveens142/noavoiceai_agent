"""
bot.py
Pipecat Voice Agent - Bot Module
Uses Daily.co transport (official Pipecat pattern).
Works with bot_runner.py for HTTP handling.

TranscriptBroadcaster sends app-messages to Daily room so the
client UI can display conversation in real-time, chunk by chunk.
Text input from the client is injected into the pipeline as a
TranscriptionFrame so the LLM responds to typed messages too.
"""

from __future__ import annotations
import json
import warnings
import asyncio
from dataclasses import dataclass, field
from typing import Optional

warnings.filterwarnings("ignore", message=".*PyTorch was not found.*")

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameProcessor

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
from pipecat.transports.daily.transport import DailyTransport, DailyParams

# Frames
from pipecat.frames.frames import (
    Frame,
    LLMRunFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
    TextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)

# LLM function call support
from pipecat.services.llm_service import FunctionCallParams

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.prompts import get_system_prompt
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

GREETING_PROMPT       = "Please say hello and briefly introduce yourself. Keep it friendly and concise."
API_LOGIN_RETRIES     = 2
API_LOGIN_RETRY_DELAY = 3.0


# ─── Immutable app-level config ───────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    agent_name: str
    system_prompt: str
    first_message: str
    openai_model: str = field(default_factory=lambda: settings.openai_model)
    tts_voice: str    = field(default_factory=lambda: settings.tts_voice)


_app_config: Optional[AppConfig] = None


# ─── Daily app-message sender helper ─────────────────────────────────────────

def _daily_send_app_message(transport: DailyTransport, payload: dict, session_id: str) -> None:
    """Send app message via Daily transport to all room participants."""
    data = json.dumps(payload)
    try:
        # Try method 1: Direct transport method
        if hasattr(transport, 'send_app_message'):
            transport.send_app_message(data, None)
            logger.debug(f"[{session_id}] ✅ App-message sent via transport.send_app_message")
            return
        
        # Try method 2: Via input transport
        input_transport = getattr(transport, '_input', None)
        if input_transport and hasattr(input_transport, 'send_app_message'):
            input_transport.send_app_message(data, None)
            logger.debug(f"[{session_id}] ✅ App-message sent via transport._input.send_app_message")
            return
        
        # Try method 3: Via output transport (original approach)
        output_transport = getattr(transport, '_output', None)
        if output_transport:
            # Try the output transport client
            if hasattr(output_transport, 'send_app_message'):
                output_transport.send_app_message(data, None)
                logger.debug(f"[{session_id}] ✅ App-message sent via transport._output.send_app_message")
                return
            
            # Try via internal _client
            client = getattr(output_transport, '_client', None)
            if client and hasattr(client, 'send_app_message'):
                client.send_app_message(data, None)
                logger.debug(f"[{session_id}] ✅ App-message sent via transport._output._client.send_app_message")
                return
        
        # If nothing worked, log all available info
        logger.warning(f"[{session_id}] ⚠️ Could not send app-message - no suitable method found")
        logger.debug(f"[{session_id}] Transport type: {type(transport)}")
        logger.debug(f"[{session_id}] Transport methods: {[m for m in dir(transport) if 'message' in m.lower()]}")
        
    except Exception as exc:
        logger.error(f"[{session_id}] ❌ Failed to send app-message: {type(exc).__name__}: {exc}", exc_info=True)


# ─── Transcript broadcaster ───────────────────────────────────────────────────

class TranscriptBroadcaster(FrameProcessor):
    """
    Intercepts STT and LLM text frames and broadcasts them as Daily
    app-messages so the client can render the chat transcript live.

    Message types sent:
      { type: "user_transcript", text: "...", final: true/false }
      { type: "bot_text_start" }
      { type: "bot_text_chunk", text: "..." }
      { type: "bot_text_done" }
    """

    def __init__(self, transport: DailyTransport, session_id: str, **kwargs):
        super().__init__(**kwargs)
        self._transport  = transport
        self._session_id = session_id

    def _send(self, payload: dict) -> None:
        _daily_send_app_message(self._transport, payload, self._session_id)

    async def process_frame(self, frame: Frame, direction) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.debug(f"[{self._session_id}] STT final: {text!r}")
                self._send({"type": "user_transcript", "text": text, "final": True})

        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                self._send({"type": "user_transcript", "text": text, "final": False})

        elif isinstance(frame, LLMFullResponseStartFrame):
            self._send({"type": "bot_text_start"})

        elif isinstance(frame, TextFrame):
            text = frame.text or ""
            if text:
                self._send({"type": "bot_text_chunk", "text": text})

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._send({"type": "bot_text_done"})

        await self.push_frame(frame, direction)


# ─── Tool schema builder ──────────────────────────────────────────────────────

def _build_tools_schema(booking_tools: BookingTools) -> ToolsSchema:
    raw_tools = booking_tools.get_tools_definition()
    schemas = []
    for tool in raw_tools:
        fn     = tool["function"]
        params = fn.get("parameters", {})
        schemas.append(
            FunctionSchema(
                name=fn["name"],
                description=fn.get("description", ""),
                properties=params.get("properties", {}),
                required=params.get("required", []),
            )
        )
    logger.debug(f"ToolsSchema: {len(schemas)} tools — {[s.name for s in schemas]}")
    return ToolsSchema(standard_tools=schemas)


# ─── Tool handler registration ────────────────────────────────────────────────

def _register_tool_handlers(
    llm: OpenAILLMService,
    booking_tools: BookingTools,
    session_id: str,
) -> None:
    read_only_tools = {"get_available_slots", "get_booking"}
    tool_names = [s["function"]["name"] for s in booking_tools.get_tools_definition()]
    logger.info(f"[{session_id}] Registering {len(tool_names)} tools: {tool_names}")

    for tool_name in tool_names:
        def make_handler(name: str):
            async def _handler(params: FunctionCallParams):
                logger.info(f"[{session_id}] Tool: {name} | args={dict(params.arguments)}")
                try:
                    result = await booking_tools.handle_tool_call(name, dict(params.arguments))
                except Exception as exc:
                    logger.error(f"[{session_id}] Tool exception {name}: {exc}", exc_info=True)
                    await params.result_callback({"error": "Internal tool error", "code": "EXCEPTION"})
                    return
                if result.get("status") == "success":
                    await params.result_callback(result["data"])
                else:
                    await params.result_callback({
                        "error": result.get("message", "Unknown error"),
                        "code":  result.get("error"),
                    })
            return _handler

        try:
            llm.register_function(
                tool_name,
                make_handler(tool_name),
                cancel_on_interruption=(tool_name in read_only_tools),
            )
        except Exception as exc:
            logger.error(f"[{session_id}] Failed to register '{tool_name}': {exc}", exc_info=True)
            raise


# ─── Greeting helper ──────────────────────────────────────────────────────────

async def _queue_greeting(task: PipelineTask, context: LLMContext) -> None:
    messages = context.get_messages()
    messages.append({"role": "system", "content": GREETING_PROMPT})
    context.set_messages(messages)
    await task.queue_frames([LLMRunFrame()])


# ─── API client with retry ────────────────────────────────────────────────────

async def _init_api_client(session_id: str) -> tuple[APIClient, Optional[BookingTools]]:
    api_client = APIClient()
    await api_client.open()
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(1, API_LOGIN_RETRIES + 1):
        try:
            await api_client.login()
            booking_tools = BookingTools(api_client)
            logger.info(f"[{session_id}] API ready (attempt {attempt})")
            return api_client, booking_tools
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[{session_id}] API login attempt {attempt} failed: {exc}")
            if attempt < API_LOGIN_RETRIES:
                await asyncio.sleep(API_LOGIN_RETRY_DELAY)
    logger.error(f"[{session_id}] All API logins failed — no tools: {last_exc}")
    return api_client, None


# ─── Pipeline builder ─────────────────────────────────────────────────────────

async def _build_pipeline(
    transport: DailyTransport,
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

    # One broadcaster instance — placed twice in the pipeline
    broadcaster = TranscriptBroadcaster(transport=transport, session_id=session_id)

    if booking_tools:
        _register_tool_handlers(llm, booking_tools, session_id)
    else:
        logger.warning(f"[{session_id}] No tools — conversation-only mode")

    context_messages = [{"role": "system", "content": _app_config.system_prompt}]

    if booking_tools:
        tools_schema = _build_tools_schema(booking_tools)
        context = LLMContext(messages=context_messages, tools=tools_schema)
        logger.info(f"[{session_id}] LLMContext: {len(tools_schema.standard_tools)} tools")
    else:
        context = LLMContext(messages=context_messages)
        logger.warning(f"[{session_id}] LLMContext has NO tools")

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        broadcaster,           # ← captures user STT frames
        user_aggregator,
        llm,
        broadcaster,           # ← captures bot LLM text frames
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


# ─── bot() ────────────────────────────────────────────────────────────────────

async def bot(transport: DailyTransport) -> None:
    assert _app_config is not None, "bot() called before initialize_app()"

    try:
        room_url   = getattr(transport, "room_url", "") or ""
        session_id = room_url.rstrip("/").split("/")[-1] or "bot-session"
    except Exception:
        session_id = "bot-session"

    logger.info(f"[{session_id}] ═══ SESSION START ═══")

    api_client, booking_tools = await _init_api_client(session_id)
    task, context = await _build_pipeline(transport, session_id, booking_tools)

    _greeted = False

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        nonlocal _greeted
        if _greeted:
            return
        _greeted = True
        logger.info(f"[{session_id}] First participant joined — greeting")
        await _queue_greeting(task, context)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal _greeted
        if _greeted:
            return
        _greeted = True
        logger.info(f"[{session_id}] Client connected — greeting")
        await _queue_greeting(task, context)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.error(f"[{session_id}] ⚠️ CLIENT DISCONNECTED - cancelling task")
        await task.cancel()
        context.set_messages([{"role": "system", "content": _app_config.system_prompt}])

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        try:
            data = json.loads(message) if isinstance(message, str) else message
            # Handle RTVI-wrapped text input
            if data.get("type") == "user-llm-text":
                text = (data.get("data", {}).get("text") or "").strip()
                if text:
                    logger.info(f"[{session_id}] Text input: {text!r}")
                    frame = TranscriptionFrame(
                        text=text,
                        user_id=sender or "user",
                        timestamp="",
                    )
                    await task.queue_frames([frame])
        except Exception as exc:
            logger.warning(f"[{session_id}] on_app_message error: {exc}")

        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        finally:
            logger.info(f"[{session_id}] Releasing API client")
            try:
                await asyncio.wait_for(api_client.close(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{session_id}] API client close timed out")
            except Exception as exc:
                logger.warning(f"[{session_id}] API client close error: {exc}")
            logger.info(f"[{session_id}] ═══ SESSION END ═══")


# ─── initialize_app() ─────────────────────────────────────────────────────────

async def initialize_app() -> None:
    global _app_config

    validate_settings()
    logger.info("═" * 60)
    logger.info("PIPECAT AGENT STARTUP (Daily.co Transport)")
    logger.info("═" * 60)

    agent_name    = settings.agent_name
    system_prompt = get_system_prompt("default")
    first_message = "Hello! Thank you for calling. How can I help you today?"
    tool_count    = 0

    startup_client = APIClient()
    try:
        await startup_client.open()
        await startup_client.login()
        logger.info("Startup auth OK")

        config_data   = await startup_client.get_agent_config()
        agent_name    = config_data.get("name",          agent_name)
        system_prompt = config_data.get("system_prompt", system_prompt)
        first_message = config_data.get("first_message", first_message)
        logger.info(f"Agent config: {agent_name}")

        validation_tools = BookingTools(startup_client)
        test_schema      = _build_tools_schema(validation_tools)
        tool_count       = len(test_schema.standard_tools)
        logger.info(f"Tool schema OK: {tool_count} tools")

    except Exception as exc:
        logger.warning(f"Startup API failed ({exc}) — using defaults")
    finally:
        try:
            await asyncio.wait_for(startup_client.close(), timeout=5.0)
        except Exception:
            pass

    if tool_count == 0:
        logger.warning("0 tools — starting in conversation-only mode")

    _app_config = AppConfig(
        agent_name=agent_name,
        system_prompt=system_prompt,
        first_message=first_message,
    )

    logger.info(f"Agent : {_app_config.agent_name}")
    logger.info(f"Model : {_app_config.openai_model}")
    logger.info(f"Voice : {_app_config.tts_voice}")
    logger.info(f"Tools : {tool_count}")
    logger.info("═" * 60)
    logger.info("READY")
    logger.info("═" * 60)