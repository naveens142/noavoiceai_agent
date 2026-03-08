"""
main.py
Production Pipecat Agent — Fixed based on official docs

ROOT CAUSE FIXES:
  1. __main__ block now awaits create_app() BEFORE pipecat_main().
     Previously create_app() was never called, so _booking_tools was
     always None and every session silently had no tools.

  2. VAD moved to TransportParams (official docs pattern).
     It was incorrectly placed in LLMUserAggregatorParams.

  3. ToolsSchema now receives FunctionSchema objects, not raw dicts.
     Raw OpenAI dicts are silently rejected by ToolsSchema.

  4. Plain LLMContext is correct per official docs — ToolsSchema's
     adapter layer handles OpenAI-specific formatting automatically.
     (OpenAILLMContext is NOT needed.)

RUNNING:
  python -m agent.main     ← correct
  DO NOT run uvicorn directly — pipecat_main() starts it internally.
"""

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

# Context — plain LLMContext per official docs.
# ToolsSchema adapter handles provider-specific conversion automatically.
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)

# FunctionSchema + ToolsSchema — required. Raw dicts are NOT accepted by ToolsSchema.
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# Pipecat services
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

# VAD — belongs in TransportParams per official docs
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
from agent.prompts import get_system_prompt, get_first_message
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)

# -------------------------------------------------------
# Module-level state — loaded once by create_app()
# -------------------------------------------------------
_agent_config: dict = {}
_booking_tools: BookingTools | None = None
_persistent_api_client: APIClient | None = None


# -------------------------------------------------------
# Helper: convert raw OpenAI dicts → ToolsSchema
# -------------------------------------------------------
def _build_tools_schema(booking_tools: BookingTools) -> ToolsSchema:
    """
    BookingTools.get_tools_definition() returns raw OpenAI-format dicts.
    ToolsSchema ONLY accepts FunctionSchema objects — raw dicts are rejected
    silently (TypeError), leaving the LLM with no tools at all.
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
    logger.info(f"✅ ToolsSchema built with {len(schemas)} tools: {names}")
    return ToolsSchema(standard_tools=schemas)


# -------------------------------------------------------
# Helper: register tool handlers on the LLM service
# -------------------------------------------------------
def _register_tool_handlers(llm: OpenAILLMService, booking_tools: BookingTools) -> None:
    read_only_tools = {"get_available_slots", "get_booking"}

    tool_names = [
        schema["function"]["name"]
        for schema in booking_tools.get_tools_definition()
    ]

    logger.info(f"\n🔧 REGISTERING {len(tool_names)} TOOL HANDLERS: {tool_names}")

    for tool_name in tool_names:

        # CRITICAL: Handler must have EXACTLY 1 parameter (FunctionCallParams).
        # Pipecat inspects the signature to detect old vs new API.
        # Any extra params (even default args) make it think it's the old 6-arg
        # signature and calls it with 6 positional args — causing the crash.
        # Capture tool_name via an immediately-invoked factory instead.

        def make_handler(name: str):
            async def _handler(params: FunctionCallParams):
                logger.critical(f"\n🔴 TOOL HANDLER CALLED: {name}")
                logger.info(f"   Arguments: {dict(params.arguments)}")

                result = await booking_tools.handle_tool_call(name, dict(params.arguments))

                if result.get("status") == "success":
                    logger.info(f"✅ Tool success: {name}")
                    await params.result_callback(result["data"])
                else:
                    logger.warning(f"❌ Tool error: {name} — {result.get('message')}")
                    await params.result_callback({
                        "error": result.get("message", "Unknown error"),
                        "code": result.get("error"),
                    })
            return _handler

        try:
            llm.register_function(
                tool_name,
                make_handler(tool_name),  # ← factory binds name, handler has 1 param
                cancel_on_interruption=(tool_name in read_only_tools),
            )
            logger.info(f"   ✓ '{tool_name}' registered")
        except Exception as e:
            logger.error(f"   ✗ FAILED '{tool_name}': {e}", exc_info=True)

# -------------------------------------------------------
# bot() — called per browser connection by pipecat runner
# -------------------------------------------------------
async def bot(runner_args: SmallWebRTCRunnerArguments):
    """Entry point called by the pipecat runner for each new connection."""

    webrtc_connection = runner_args.webrtc_connection
    system_prompt = _agent_config.get("system_prompt", get_system_prompt("default"))

    logger.info("New call session started")

    # ------------------------------------------------------------------
    # 1. Transport — VAD belongs HERE per official docs.
    #    Not in LLMUserAggregatorParams.
    # ------------------------------------------------------------------
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),  # ← correct location
        ),
    )

    # ------------------------------------------------------------------
    # 2. STT
    # ------------------------------------------------------------------
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        model="nova-2",
        language="en",
        smart_format=True,
        interim_results=True,
    )

    # ------------------------------------------------------------------
    # 3. LLM
    # ------------------------------------------------------------------
    llm = OpenAILLMService(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
    )

    # ------------------------------------------------------------------
    # 4. TTS
    # ------------------------------------------------------------------
    tts = OpenAITTSService(
        api_key=settings.openai_api_key,
        voice=settings.tts_voice
    )

    # ------------------------------------------------------------------
    # 5. Register tool handlers on the LLM service
    # ------------------------------------------------------------------
    if _booking_tools:
        _register_tool_handlers(llm, _booking_tools)
    else:
        logger.warning("⚠️  _booking_tools is None!")
        logger.warning("    create_app() was not awaited before pipecat_main().")

    # ------------------------------------------------------------------
    # 6. LLMContext with ToolsSchema
    #    Per official docs: LLMContext + ToolsSchema is the correct pattern.
    # ------------------------------------------------------------------
    context_messages = [{"role": "system", "content": system_prompt}]

    if _booking_tools:
        tools_schema = _build_tools_schema(_booking_tools)
        context = LLMContext(messages=context_messages, tools=tools_schema)
        logger.info("✅ LLMContext created with ToolsSchema")
    else:
        context = LLMContext(messages=context_messages)
        logger.warning("⚠️  LLMContext created WITHOUT tools")

    # ------------------------------------------------------------------
    # 7. Aggregators — no VAD params here, VAD is on transport
    # ------------------------------------------------------------------
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    # ------------------------------------------------------------------
    # 8. Pipeline
    # ------------------------------------------------------------------
    pipeline = Pipeline([
        transport.input(),       # Audio in (VAD gating via TransportParams)
        stt,                     # Speech → Text
        user_aggregator,         # User message → context
        llm,                     # LLM inference + tool dispatch
        tts,                     # Text → Speech
        transport.output(),      # Audio out
        assistant_aggregator,    # Assistant + tool results → context
    ])

    # ------------------------------------------------------------------
    # 9. Task
    # ------------------------------------------------------------------
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # ------------------------------------------------------------------
    # 10. Event handlers
    # ------------------------------------------------------------------
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.critical(f"\n{'='*60}\n🔴 CLIENT CONNECTED\n{'='*60}")
        current_messages = context.get_messages()
        current_messages.append({
            "role": "system",
            "content": (
                "Please say hello and briefly introduce yourself. "
                "Keep it friendly and concise."
            ),
        })
        context.set_messages(current_messages)
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()
        context.set_messages([{"role": "system", "content": system_prompt}])

    # ------------------------------------------------------------------
    # 11. Run
    # ------------------------------------------------------------------
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    try:
        context.set_messages([{"role": "system", "content": system_prompt}])
    except Exception as e:
        logger.warning(f"Cleanup warning: {e}")


# -------------------------------------------------------
# create_app — one-time startup, must run before pipecat_main()
# -------------------------------------------------------
async def create_app():
    """
    Authenticate, load agent config, and initialise booking tools.

    MUST be awaited before pipecat_main() — see __main__ block below.
    """
    global _agent_config, _booking_tools, _persistent_api_client

    validate_settings()
    logger.info("\n" + "=" * 60)
    logger.info("PIPECAT AGENT INITIALIZATION")
    logger.info("=" * 60)

    # Keep client alive for process lifetime.
    # Never use `async with` — it closes httpx.AsyncClient after startup.
    _persistent_api_client = APIClient()
    await _persistent_api_client.open()

    try:
        await _persistent_api_client.login()
        logger.info("✅ Auth successful")

        _agent_config = await _persistent_api_client.get_agent_config()
        logger.info(f"✅ Config loaded: {_agent_config.get('name')}")

    except Exception as e:
        logger.warning(f"⚠️  API init failed: {e} — using defaults")
        _agent_config = {
            "name": settings.agent_name,
            "system_prompt": (
                "You are a helpful medical receptionist. "
                "Help users book appointments and answer questions."
            ),
            "first_message": "Hello! Thank you for calling. How can I help you today?",
        }

    _booking_tools = BookingTools(_persistent_api_client)

    # Validate at startup — fail loudly rather than silently at call time
    try:
        test_schema = _build_tools_schema(_booking_tools)
        tool_count = len(test_schema.standard_tools)
        logger.info(f"✅ Tool schema OK: {tool_count} tools")
    except Exception as e:
        logger.error(f"❌ TOOL SCHEMA FAILED: {e}", exc_info=True)
        raise

    logger.info(f"Agent: {_agent_config.get('name', settings.agent_name)}")
    logger.info(f"Model: {settings.openai_model} | Voice: {settings.tts_voice}")
    logger.info(f"Tools: {tool_count}")
    logger.info("=" * 60)
    logger.info("READY — http://localhost:7860/client")
    logger.info("=" * 60 + "\n")

    return bot


# -------------------------------------------------------
# Entry point: python -m agent.main
#
# CRITICAL ORDER:
#   1. asyncio.run(create_app())  — init tools, auth, config
#   2. pipecat_main()             — start uvicorn + WebRTC server
#
# DO NOT run uvicorn separately. pipecat_main() handles it.
# -------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    asyncio.run(create_app())   # ← Must complete before runner starts
    pipecat_main()              # ← Starts uvicorn internally