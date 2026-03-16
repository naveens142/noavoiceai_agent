"""bot_runner.py
Pipecat Bot Runner - SmallWebRTC Transport
Uses WebRTC for peer-to-peer communication.
Works locally AND in production (Google Cloud Run, Render, etc).
"""

import os
import sys
import asyncio
import argparse
import socket
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from loguru import logger

# Load environment FIRST before any os.getenv calls
load_dotenv(override=True)

from agent.config import settings
from bot import initialize_app, bot

# ── Official Pipecat SmallWebRTC imports ──────────────────────────────────────
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection, IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)


# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.getenv("LOG_FILE",  "/tmp/pipecat.log")

try:
    logger.remove()
except Exception:
    pass

logger.add(sys.stderr, level=LOG_LEVEL, format="<level>{level: <8}</level> | {message}")
logger.add(
    LOG_FILE,
    level=LOG_LEVEL,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    rotation="100 MB",
    retention="7 days",
)


# ─── Configuration ─────────────────────────────────────────────────────────────

MAX_CONCURRENT_BOTS = int(os.getenv("MAX_CONCURRENT_BOTS", "20"))
PORT               = int(os.getenv("PORT", "7860"))
HOST               = os.getenv("HOST", "0.0.0.0")

active_sessions: dict = {}


# ─── ICE/TURN Server Configuration ─────────────────────────────────────────────

def _get_ice_servers() -> Optional[List[IceServer]]:
    """Get ICE servers for NAT traversal."""
    turn_url = os.getenv("METERED_TURN_URL")
    username  = os.getenv("METERED_TURN_USERNAME")
    credential = os.getenv("METERED_TURN_CREDENTIAL")

    if not turn_url or not username or not credential:
        logger.info("No TURN configured — using default STUN")
        return None

    logger.info(f"TURN configured: {turn_url}")
    return [
        IceServer(urls="stun:stun.relay.metered.ca:80"),
        IceServer(urls=f"turn:{turn_url}:80",               username=username, credential=credential),
        IceServer(urls=f"turn:{turn_url}:80?transport=tcp", username=username, credential=credential),
        IceServer(urls=f"turn:{turn_url}:443",              username=username, credential=credential),
        IceServer(urls=f"turns:{turn_url}:443?transport=tcp", username=username, credential=credential),
    ]


# ─── SmallWebRTCRequestHandler (singleton) ────────────────────────────────────
#
# This is Pipecat's official helper. It:
#   1. Calls connection.initialize(offer_sdp) — sets up tracks, data channel, etc.
#   2. Waits for ICE gathering to complete.
#   3. Returns the fully-formed SDP answer in one shot.
#   4. Manages connection re-use (pc_id) for ICE restarts.

_webrtc_handler: Optional[SmallWebRTCRequestHandler] = None


def get_handler() -> SmallWebRTCRequestHandler:
    global _webrtc_handler
    if _webrtc_handler is None:
        _webrtc_handler = SmallWebRTCRequestHandler(
            ice_servers=_get_ice_servers(),
        )
    return _webrtc_handler


# ─── FastAPI Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("╔" + "═" * 58 + "╗")
    logger.info("║ PIPECAT BOT RUNNER (SmallWebRTC Transport)             ║")
    logger.info("╚" + "═" * 58 + "╝")

    # Eagerly create the handler so ICE config is validated at startup
    get_handler()

    try:
        logger.info("🔄 Initializing bot app (this may take 10-20 seconds)...")
        await asyncio.wait_for(initialize_app(), timeout=30.0)
        logger.info("✅ Bot app initialized")
    except asyncio.TimeoutError:
        logger.error("❌ App init timeout (>30s) — check API connectivity")
        logger.warning("⚠️ Using defaults — tools may not be available")
    except Exception as exc:
        logger.error(f"❌ App init failed: {exc}")
        logger.warning("⚠️ Using defaults — tools may not be available")

    logger.info("🎙️  SmallWebRTC transport (peer-to-peer, no external SaaS)")
    logger.info(f"✅ HTTP server ready on {HOST}:{PORT}")
    logger.info(f"📊 Max concurrent bots: {MAX_CONCURRENT_BOTS}")

    yield

    logger.info("🛑 Shutting down — cleaning up sessions...")
    active_sessions.clear()
    logger.info("✅ Shutdown complete")


# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Pipecat Bot Runner (SmallWebRTC)",
    description="Voice agent with SmallWebRTC peer-to-peer transport",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client")
if os.path.isdir(client_dir):
    try:
        app.mount("/static", StaticFiles(directory=client_dir), name="static")
        logger.info("📁 Mounted client directory")
    except Exception as exc:
        logger.warning(f"⚠️ Could not mount client: {exc}")


# ─── Health & status ───────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return JSONResponse({
        "status": "healthy",
        "active_sessions": len(active_sessions),
        "max_concurrent": MAX_CONCURRENT_BOTS,
        "transport": "smallwebrtc",
    })


@app.get("/status")
async def status():
    return JSONResponse({
        "active_sessions": len(active_sessions),
        "max_concurrent": MAX_CONCURRENT_BOTS,
        "sessions": list(active_sessions.keys()),
        "transport": "smallwebrtc",
    })


@app.get("/sessions")
async def list_sessions():
    return JSONResponse({
        "active_sessions": [
            {
                "session_id": sid,
                "status": data.get("status", "unknown"),
                "created_at": data.get("created_at", 0),
            }
            for sid, data in active_sessions.items()
        ],
        "total": len(active_sessions),
        "max_concurrent": MAX_CONCURRENT_BOTS,
    })


# ─── WebRTC Signaling — single /offer endpoint ────────────────────────────────
#
# KEY FIX: Use SmallWebRTCRequestHandler.handle_web_request().
#
# This replaces the old split /offer + polling /answer pattern.
# The answer SDP is returned synchronously in the same HTTP response,
# which is what all standard WebRTC clients (and the Pipecat JS SDK) expect.

@app.post("/offer")
async def handle_offer(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Receive WebRTC SDP offer, return SDP answer in the same response.
    The bot is spawned as a background task once the connection is initialized.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if len(active_sessions) >= MAX_CONCURRENT_BOTS:
        raise HTTPException(status_code=503, detail="Maximum concurrent sessions reached")

    # Build the SmallWebRTCRequest pydantic model the handler expects
    try:
        webrtc_request = SmallWebRTCRequest(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid WebRTC request: {exc}")

    handler = get_handler()

    # webrtc_connection_callback is called by the handler AFTER the connection
    # is fully initialized (tracks registered, ICE gathered).  We spawn the bot
    # pipeline here so it starts AFTER the transport is ready.
    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        session_id = connection.pc_id

        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(
                audio_in_enabled=True,   # ← REQUIRED: receive mic audio
                audio_out_enabled=True,  # ← REQUIRED: send TTS audio back
                audio_out_10ms_chunks=2, # smoother audio delivery
            ),
        )

        active_sessions[session_id] = {
            "status": "running",
            "created_at": asyncio.get_event_loop().time(),
        }
        logger.info(f"[{session_id}] 🤖 Spawning bot task...")

        # Run bot as background task so we don't block the HTTP response
        background_tasks.add_task(_run_bot_session, transport, session_id)

    # handle_web_request() does the full SDP handshake and returns the answer
    try:
        answer = await handler.handle_web_request(
            request=webrtc_request,
            webrtc_connection_callback=webrtc_connection_callback,
        )
    except Exception as exc:
        logger.error(f"WebRTC handshake failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if answer is None:
        raise HTTPException(status_code=500, detail="No SDP answer generated")

    # answer is a dict with keys: sdp, type, pc_id
    return JSONResponse(answer)


# ─── Bot session runner ────────────────────────────────────────────────────────

async def _run_bot_session(transport: SmallWebRTCTransport, session_id: str) -> None:
    """Run bot pipeline for one session, clean up on exit."""
    try:
        await bot(transport, session_id)
        logger.info(f"[{session_id}] ✅ Bot session completed normally")
    except Exception as exc:
        logger.error(f"[{session_id}] ❌ Bot session error: {type(exc).__name__}: {exc}", exc_info=True)
    finally:
        active_sessions.pop(session_id, None)
        logger.info(f"[{session_id}] 🧹 Session cleaned up")


# ─── Stop bot ──────────────────────────────────────────────────────────────────

@app.post("/stop_bot/{session_id}")
async def stop_bot(session_id: str) -> JSONResponse:
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    active_sessions.pop(session_id, None)
    logger.info(f"[{session_id}] 🛑 Session stopped via API")
    return JSONResponse({"status": "success", "session_id": session_id})


# ─── Root / landing page ───────────────────────────────────────────────────────

@app.get("/")
async def root():
    for filename in ("webrtc-client.html", "daily-client.html", "index.html"):
        path = os.path.join(client_dir, filename)
        if os.path.isfile(path):
            return FileResponse(path, media_type="text/html")

    return HTMLResponse("""
    <html><body style="font-family:Arial;max-width:600px;margin:50px auto;">
        <h1>🎙️ Pipecat Voice Bot</h1>
        <p>✅ Server running (SmallWebRTC transport)</p>
        <p>POST SDP offer to <code>/offer</code> to start a session.</p>
        <p><a href="/health">Health</a> | <a href="/docs">API Docs</a></p>
    </body></html>
    """)


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # Allow fast restart without "address already in use"
    _orig_socket = socket.socket
    class _ReuseSocket(socket.socket):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            try:
                self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (OSError, AttributeError):
                pass
    socket.socket = _ReuseSocket

    parser = argparse.ArgumentParser(description="Pipecat Bot Runner (SmallWebRTC)")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    logger.info(f"Starting on {args.host}:{args.port}")
    try:
        uvicorn.run(
            "bot_runner:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_config=None,
            server_header=False,
            access_log=False,
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")