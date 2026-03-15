"""
bot.py
Pipecat Voice Agent - Bot Module
Uses Daily.co transport (official Pipecat pattern).
Works with bot_runner.py for HTTP + WebSocket handling.
"""

from __future__ import annotations
import os
import warnings
import asyncio
from dataclasses import dataclass, field
from typing import Optional

warnings.filterwarnings("ignore", message=".*PyTorch was not found.*")

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

# Transport - Daily.co (official Pipecat pattern)
from pipecat.transports.daily.transport import DailyTransport, DailyParams
from pipecat.transports.base_transport import TransportParams

# Frames
from pipecat.frames.frames import (
    LLMRunFrame, Frame, LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    TranscriptionFrame, InterimTranscriptionFrame
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame, DailyOutputTransportMessageUrgentFrame

# LLM function call support
from pipecat.services.llm_service import FunctionCallParams

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.prompts import get_system_prompt
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)


# ─── Custom Text Capture Processor ────────────────────────────────────────────

class TextCaptureProcessor(FrameProcessor):
    """Capture LLM text output and send to client via app-messages"""
    
    def __init__(self, session_id: str, task):
        super().__init__()
        self.session_id = session_id
        self.task = task
        self.in_response = False
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Intercept LLMTextFrame and send to client"""
        await super().process_frame(frame, direction)
        
        # Detect start of LLM response
        if isinstance(frame, LLMFullResponseStartFrame):
            self.in_response = True
            logger.info("[%s] ✅ LLM response started", self.session_id)
            try:
                msg = DailyOutputTransportMessageUrgentFrame(
                    message={"type": "bot_text_start", "text": ""}
                )
                logger.info("[%s] Queuing URGENT app-message: bot_text_start", self.session_id)
                await self.task.queue_frame(msg)
                logger.info("[%s] ✅ Queued bot_text_start", self.session_id)
            except Exception as e:
                logger.error("[%s] ❌ Could not queue bot_text_start: %s", self.session_id, e, exc_info=True)
        
        # Capture text chunks
        elif isinstance(frame, LLMTextFrame) and self.in_response:
            try:
                text = frame.text
                if text:
                    logger.info("[%s] 📝 Capturing LLM text chunk: %r (%d chars)", self.session_id, text[:30], len(text))
                    msg = DailyOutputTransportMessageUrgentFrame(
                        message={"type": "bot_text_chunk", "text": text}
                    )
                    await self.task.queue_frame(msg)
                    logger.info("[%s] ✅ Sent bot_text_chunk (%d chars) — URGENT (real-time)", self.session_id, len(text))
            except Exception as e:
                logger.error("[%s] ❌ Text chunk queue error: %s", self.session_id, e, exc_info=True)
        
        # Detect end of LLM response
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self.in_response:
                self.in_response = False
                logger.info("[%s] ✅ LLM response ended", self.session_id)
                try:
                    msg = DailyOutputTransportMessageUrgentFrame(
                        message={"type": "bot_text_done", "text": ""}
                    )
                    logger.info("[%s] Queuing URGENT app-message: bot_text_done", self.session_id)
                    await self.task.queue_frame(msg)
                    logger.info("[%s] ✅ Queued bot_text_done", self.session_id)
                except Exception as e:
                    logger.error("[%s] ❌ Could not queue bot_text_done: %s", self.session_id, e, exc_info=True)
        
        await self.push_frame(frame, direction)


# ─── Speech Capture Processor ─────────────────────────────────────────────────

class SpeechCaptureProcessor(FrameProcessor):
    """Capture STT (speech-to-text) output and send to client for display"""
    
    def __init__(self, session_id: str, task):
        super().__init__()
        self.session_id = session_id
        self.task = task
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Intercept transcription frames and send to client"""
        await super().process_frame(frame, direction)
        
        # Capture interim transcription (partial STT results)
        if isinstance(frame, InterimTranscriptionFrame):
            try:
                text = frame.text if hasattr(frame, 'text') else ''
                if text:
                    logger.info("[%s] 🗣️ Interim STT: %r", self.session_id, text)
                    msg = DailyOutputTransportMessageUrgentFrame(
                        message={
                            "type": "user_transcript",
                            "text": text,
                            "final": False
                        }
                    )
                    await self.task.queue_frame(msg)
                    logger.info("[%s] ✅ Sent interim transcript", self.session_id)
            except Exception as e:
                logger.error("[%s] ❌ Interim speech capture error: %s", self.session_id, e, exc_info=True)
        
        # Capture final transcription (complete STT result)
        elif isinstance(frame, TranscriptionFrame):
            try:
                text = frame.text if hasattr(frame, 'text') else ''
                if text:
                    logger.info("[%s] 🗣️ Final STT: %r", self.session_id, text)
                    msg = DailyOutputTransportMessageUrgentFrame(
                        message={
                            "type": "user_transcript",
                            "text": text,
                            "final": True
                        }
                    )
                    await self.task.queue_frame(msg)
                    logger.info("[%s] ✅ Sent final transcript", self.session_id)
            except Exception as e:
                logger.error("[%s] ❌ Final speech capture error: %s", self.session_id, e, exc_info=True)
        
        await self.push_frame(frame, direction)


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
    """Build Pipecat ToolsSchema from BookingTools"""
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
    """Register BookingTools with OpenAI LLM for function calling"""
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


# ─── Core pipeline builder (transport-agnostic) ──────────────────────────────

async def _build_pipeline(
    transport: DailyTransport,
    session_id: str,
    booking_tools: Optional[BookingTools],
) -> tuple[PipelineTask, LLMContext]:
    """Build Pipecat audio pipeline with STT → LLM → TTS"""

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
        language=None,
    )

    # Create placeholder text capturer (will be linked to task after task creation)
    text_capturer = TextCaptureProcessor(session_id, task=None)
    
    # Create placeholder speech capturer (will be linked to task after task creation)
    speech_capturer = SpeechCaptureProcessor(session_id, task=None)

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
        speech_capturer,  # Capture STT output for chat display
        user_aggregator,
        llm,
        text_capturer,  # Capture LLM output for chat
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

    # Link task to both capturers now that it exists
    text_capturer.task = task
    speech_capturer.task = task

    return task, context


# ─── bot() — Entry point called by Pipecat/bot_runner ────────────────────────

async def bot(transport: DailyTransport) -> None:
    """Main bot function - called by bot_runner with Dclear
    aily.co transport"""
    assert _app_config is not None, "bot() called before initialize_app()"

    # Extract room name from transport URL or use fallback
    try:
        # room_url is like: https://noavoiceai.daily.co/pipecat-xyz
        room_url = getattr(transport, 'room_url', '')
        if room_url:
            session_id = room_url.split('/')[-1]  # Get 'pipecat-xyz' part
        else:
            session_id = "bot-session"
    except Exception:
        session_id = "bot-session"
    
    logger.info("[%s] ═══ SESSION START (Daily.co) ═══", session_id)

    api_client = APIClient()
    booking_tools: Optional[BookingTools] = None

    try:
        await api_client.open()
        await api_client.login()
        booking_tools = BookingTools(api_client)
        logger.info("[%s] API client ready", session_id)
    except Exception as exc:
        logger.error("[%s] API client init failed: %s — no tools", session_id, exc)

    task, context = await _build_pipeline(transport, session_id, booking_tools)

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info("[%s] First participant joined — starting greeting", session_id)
        messages = context.get_messages()
        messages.append({
            "role": "system",
            "content": "Please say hello and briefly introduce yourself. Keep it friendly and concise.",
        })
        context.set_messages(messages)
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        """Handle text messages from client (e.g., chat input)"""
        logger.info("[%s] on_app_message called from %s with: %s", session_id, sender, str(message)[:100])
        try:
            msg_type = message.get("type", "")
            logger.info("[%s] Message type: %s", session_id, msg_type)
            msg_data = message.get("data", {})
            
            # Handle RTVI user-llm-text format from client
            if msg_type == "user-llm-text":
                user_text = msg_data.get("text", "")
                logger.info("[%s] Raw user text: %s", session_id, repr(user_text))
                if user_text and user_text.strip():
                    logger.info("[%s] ✅ Received text message: %s", session_id, user_text)
                    
                    # Send user text back to client for display
                    logger.info("[%s] Sending user text to client", session_id)
                    try:
                        user_display_msg = DailyOutputTransportMessageUrgentFrame(
                            message={"type": "user_transcript", "text": user_text, "final": True}
                        )
                        await task.queue_frame(user_display_msg)
                        logger.info("[%s] ✅ Queued user text for display (URGENT)", session_id)
                    except Exception as e:
                        logger.error("[%s] ❌ Could not queue user text display: %s", session_id, e, exc_info=True)
                    
                    # Add user message to context for LLM
                    messages = context.get_messages()
                    logger.debug("[%s] Context before: %d messages", session_id, len(messages))
                    messages.append({"role": "user", "content": user_text})
                    context.set_messages(messages)
                    logger.debug("[%s] Context after: %d messages", session_id, len(messages))
                    
                    # Queue LLM to generate response
                    logger.info("[%s] 🚀 Queuing LLMRunFrame", session_id)
                    await task.queue_frames([LLMRunFrame()])
                else:
                    logger.warning("[%s] Empty text message", session_id)
            else:
                logger.debug("[%s] Ignoring message type: %s", session_id, msg_type)
        except Exception as exc:
            logger.error("[%s] App message handler error: %s", session_id, exc, exc_info=True)

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


# ─── initialize_app() — Called by bot_runner at startup ──────────────────────

async def initialize_app() -> None:
    """Load and validate app configuration once at startup"""
    global _app_config

    validate_settings()

    logger.info("═" * 60)
    logger.info("PIPECAT AGENT STARTUP (Daily.co Transport)")
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
