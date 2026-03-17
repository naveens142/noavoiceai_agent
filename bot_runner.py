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

# session_id → {"status": str, "created_at": float, "task": asyncio.Task|None}
active_sessions: dict = {}


# ─── ICE/TURN Server Configuration ─────────────────────────────────────────────

# Public STUN servers — always used so the server discovers its public (reflexive) IP.
# Without these, the server only advertises its private Render IP (10.x.x.x),
# which is unreachable from the internet → ICE always fails on cloud deployments.
_STUN_SERVERS = [
    IceServer(urls="stun:stun.relay.metered.ca:80"),  # Metered STUN
    IceServer(urls="stun:stun.l.google.com:19302"),   # Google STUN (fallback)
    IceServer(urls="stun:stun1.l.google.com:19302"),  # Google STUN (fallback)
]


def _get_ice_servers() -> List[IceServer]:
    """
    Return ICE servers for NAT traversal.

    Always includes public STUN so the server discovers its public IP on cloud
    deployments (Render, Fly, GCP, etc.).

    If METERED_TURN_* env vars are set, TURN relays are added too.
    TURN (over TCP/443) is required when the cloud provider blocks UDP
    (e.g., Render's load-balancer does not forward arbitrary UDP ports).
    
    Set SKIP_TURN=1 to disable TURN for local testing (STUN only).
    """
    turn_url   = os.getenv("METERED_TURN_URL")
    username   = os.getenv("METERED_TURN_USERNAME")
    credential = os.getenv("METERED_TURN_CREDENTIAL")
    skip_turn  = os.getenv("SKIP_TURN", "0") == "1"

    if skip_turn or not turn_url or not username or not credential:
        if skip_turn:
            logger.info("ICE: SKIP_TURN=1, using STUN only (local testing mode)")
        else:
            logger.info("ICE: using STUN only (no TURN configured)")
        return _STUN_SERVERS

    logger.info(f"ICE: STUN + TURN configured via global.relay.metered.ca (metered.ca)")
    return [
        *_STUN_SERVERS,
        # Exact format from metered.ca docs: https://www.metered.ca/
        IceServer(urls="turn:global.relay.metered.ca:80",                  username=username, credential=credential),
        IceServer(urls="turn:global.relay.metered.ca:80?transport=tcp",    username=username, credential=credential),
        IceServer(urls="turn:global.relay.metered.ca:443",                 username=username, credential=credential),
        IceServer(urls="turns:global.relay.metered.ca:443?transport=tcp",  username=username, credential=credential),
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
        ice = _get_ice_servers()
        logger.info(f"WebRTC handler: {len(ice)} ICE server(s) configured")
        _webrtc_handler = SmallWebRTCRequestHandler(
            ice_servers=ice,
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


@app.get("/ice-config")
async def ice_config():
    """Return current ICE server config — useful for debugging connection failures."""
    servers = _get_ice_servers()
    has_turn = any("turn:" in (s.urls if isinstance(s.urls, str) else s.urls[0]) for s in servers)
    
    # Serialize ICE servers including username/credential for TURN
    ice_servers_list = []
    for s in servers:
        server_obj = {"urls": s.urls}
        if s.username:
            server_obj["username"] = s.username
        if s.credential:
            server_obj["credential"] = s.credential
        ice_servers_list.append(server_obj)
    
    return JSONResponse({
        "ice_servers": ice_servers_list,
        "has_turn": has_turn,
        "note": "TURN is required on Render (UDP is blocked). Add METERED_TURN_* env vars if connections fail." if not has_turn else "TURN configured.",
    })


@app.get("/ice-diagnostics")
async def ice_diagnostics():
    """Detailed ICE diagnostics for troubleshooting."""
    return JSONResponse({
        "environment": {
            "SKIP_TURN": os.getenv("SKIP_TURN", "0"),
            "METERED_TURN_URL": "***" if os.getenv("METERED_TURN_URL") else "(not set)",
            "METERED_TURN_USERNAME": "***" if os.getenv("METERED_TURN_USERNAME") else "(not set)",
            "METERED_TURN_CREDENTIAL": "***" if os.getenv("METERED_TURN_CREDENTIAL") else "(not set)",
        },
        "ice_servers": [
            {"urls": s.urls, "has_auth": bool(s.username and s.credential)}
            for s in _get_ice_servers()
        ],
        "recommendations": [
            "If 'has_turn' is false and you're on Render, get TURN credentials from https://metered.ca",
            "If TURN auth fails, verify credentials are valid at metered.ca dashboard",
            "For local testing only, set SKIP_TURN=1 to use STUN (Google STUN works locally)",
            "WebRTC 'failed' after ~15s usually means no ICE candidates could connect",
        ],
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

    # Track the session_id registered inside the callback so we can
    # clean it up if handle_web_request() raises after the callback ran.
    _registered_session_id: list = []  # mutable container for nonlocal write

    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        sid = connection.pc_id
        _registered_session_id.append(sid)

        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(
                audio_in_enabled=True,   # ← REQUIRED: receive mic audio
                audio_out_enabled=True,  # ← REQUIRED: send TTS audio back
                audio_out_10ms_chunks=2, # smoother audio delivery
            ),
        )

        active_sessions[sid] = {
            "status": "running",
            "created_at": asyncio.get_event_loop().time(),
            "task": None,  # filled in by _run_bot_session once the task starts
        }
        logger.info(f"[{sid}] 🤖 Spawning bot task...")
        background_tasks.add_task(_run_bot_session, transport, sid)

    # handle_web_request() does the full SDP handshake and returns the answer
    try:
        answer = await handler.handle_web_request(
            request=webrtc_request,
            webrtc_connection_callback=webrtc_connection_callback,
        )
    except Exception as exc:
        # Clean up any session that was registered before the failure
        for sid in _registered_session_id:
            active_sessions.pop(sid, None)
            logger.warning(f"[{sid}] 🧹 Cleaned up session after handshake failure")
        logger.error(f"WebRTC handshake failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if answer is None:
        raise HTTPException(status_code=500, detail="No SDP answer generated")

    # answer is a dict with keys: sdp, type, pc_id
    return JSONResponse(answer)


# ─── Bot session runner ────────────────────────────────────────────────────────

async def _run_bot_session(transport: SmallWebRTCTransport, session_id: str) -> None:
    """Run bot pipeline for one session, clean up on exit."""
    # Store the asyncio Task so stop_bot() can cancel it properly.
    current_task = asyncio.current_task()
    if session_id in active_sessions:
        active_sessions[session_id]["task"] = current_task
    try:
        await bot(transport, session_id)
        logger.info(f"[{session_id}] ✅ Bot session completed normally")
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] 🛑 Bot session cancelled")
    except Exception as exc:
        logger.error(f"[{session_id}] ❌ Bot session error: {type(exc).__name__}: {exc}", exc_info=True)
    finally:
        active_sessions.pop(session_id, None)
        logger.info(f"[{session_id}] 🧹 Session cleaned up")


# ─── Stop bot ──────────────────────────────────────────────────────────────────

@app.post("/stop_bot/{session_id}")
async def stop_bot(session_id: str) -> JSONResponse:
    session = active_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    # Cancel the running asyncio Task — _run_bot_session.finally() removes from active_sessions
    task: asyncio.Task | None = session.get("task")
    if task and not task.done():
        task.cancel()
        logger.info(f"[{session_id}] 🛑 Session cancelled via API")
    else:
        active_sessions.pop(session_id, None)
        logger.info(f"[{session_id}] 🛑 Session removed via API (task already done)")
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