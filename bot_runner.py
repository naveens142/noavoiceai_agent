"""
bot_runner.py
Pipecat Bot Runner - Daily.co Transport (Official Pipecat Pattern)
Uses Daily.co for WebRTC - official Pipecat recommendation.
Free: 10,000 participant-minutes per month.
Works locally AND in production (Render).
"""

import os
import sys
import asyncio
import argparse
import uuid
import httpx
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from loguru import logger

# Load environment FIRST before any os.getenv calls
load_dotenv(override=True)

from agent.config import settings
from bot import initialize_app, bot
from pipecat.transports.daily.transport import DailyTransport, DailyParams


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

DAILY_API_KEY      = os.getenv("DAILY_API_KEY", "9b747accdd8dc593c4469826c0c5127fe931d56ee304063dab4cbddad7b12567")
MAX_CONCURRENT_BOTS = int(os.getenv("MAX_CONCURRENT_BOTS", "20"))
PORT               = int(os.getenv("PORT", "7860"))
HOST               = os.getenv("HOST", "0.0.0.0")
DAILY_API_BASE     = "https://api.daily.co/v1"

active_sessions: dict = {}   # session_id → session metadata
sdk_cache:       dict = {}   # in-memory cache for Daily.co JS SDK


# ─── Daily.co API helpers ──────────────────────────────────────────────────────

def _daily_headers() -> dict:
    return {"Authorization": f"Bearer {DAILY_API_KEY}"}


async def _create_daily_room(client: httpx.AsyncClient, room_name: str) -> str:
    """Create a private Daily.co room. Returns room_url."""
    resp = await client.post(
        f"{DAILY_API_BASE}/rooms",
        json={"name": room_name, "privacy": "private"},
        headers=_daily_headers(),
    )
    if resp.status_code != 200:
        logger.error(f"Daily room creation failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=500, detail="Failed to create Daily.co room")
    return resp.json()["url"]


async def _create_daily_token(
    client: httpx.AsyncClient,
    room_name: str,
    *,
    is_owner: bool,
    user_name: str,
) -> Optional[str]:
    """Generate a Daily.co meeting token. Returns token string or None on failure."""
    resp = await client.post(
        f"{DAILY_API_BASE}/meeting-tokens",
        json={
            "properties": {
                "room_name": room_name,
                "is_owner": is_owner,
                "user_name": user_name,
                "enable_screenshare": False,
                "start_video_off": True,
            }
        },
        headers=_daily_headers(),
    )
    if resp.status_code == 200:
        return resp.json().get("token")
    logger.error(f"Daily token generation failed: {resp.status_code} {resp.text}")
    return None


# ─── FastAPI Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("╔" + "═" * 58 + "╗")
    logger.info("║ PIPECAT BOT RUNNER (Daily.co Transport)                 ║")
    logger.info("╚" + "═" * 58 + "╝")

    if not DAILY_API_KEY:
        logger.error("❌ DAILY_API_KEY not set — add it to .env or export it")
        sys.exit(1)

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

    logger.info("🎙️  Daily.co transport (official Pipecat pattern)")
    logger.info(f"✅ HTTP server ready on {HOST}:{PORT}")
    logger.info(f"📊 Max concurrent bots: {MAX_CONCURRENT_BOTS}")

    yield

    logger.info("🛑 Shutting down — cleaning up sessions...")
    active_sessions.clear()
    logger.info("✅ Shutdown complete")


# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Pipecat Bot Runner (Daily.co)",
    description="Voice agent with Daily.co WebRTC transport",
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
        "transport": "daily",
    })


@app.get("/status")
async def status():
    return JSONResponse({
        "active_sessions": len(active_sessions),
        "max_concurrent": MAX_CONCURRENT_BOTS,
        "sessions": list(active_sessions.keys()),
        "transport": "daily.co",
    })


@app.get("/sessions")
async def list_sessions():
    return JSONResponse({
        "active_sessions": [
            {
                "session_id": sid,
                "status": data.get("status", "unknown"),
                "room_url": data.get("room_url", ""),
                "created_at": data.get("created_at", 0),
            }
            for sid, data in active_sessions.items()
        ],
        "total": len(active_sessions),
        "max_concurrent": MAX_CONCURRENT_BOTS,
    })


# ─── Daily.co JS SDK proxy (CDN fallback) ──────────────────────────────────────

@app.get("/daily-js.js")
async def serve_daily_sdk():
    """Proxy/cache the Daily.co JS SDK as a CDN fallback."""
    if "daily-js" in sdk_cache:
        return Response(content=sdk_cache["daily-js"], media_type="application/javascript")

    cdn_urls = [
        "https://cdn.daily.co/daily-js.js",
        "https://unpkg.com/@daily-co/daily-js@latest/dist/daily-js.js",
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        for url in cdn_urls:
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200:
                    sdk_cache["daily-js"] = resp.text
                    logger.info(f"✅ Daily.co SDK cached from {url}")
                    return Response(content=resp.text, media_type="application/javascript")
            except Exception:
                continue

    error_js = "console.error('Daily.co SDK failed to load from all CDN sources');"
    return Response(content=error_js, media_type="application/javascript", status_code=503)


# ─── Root / landing page ───────────────────────────────────────────────────────

@app.get("/")
async def root():
    for filename in ("daily-client.html", "index.html"):
        path = os.path.join(client_dir, filename)
        if os.path.isfile(path):
            return FileResponse(path, media_type="text/html")

    return HTMLResponse("""
    <html><body style="font-family:Arial;max-width:600px;margin:50px auto;">
        <h1>🎙️ Pipecat Voice Bot</h1>
        <p>✅ Server running (Daily.co transport)</p>
        <p><a href="/health">Health</a> | <a href="/docs">API Docs</a></p>
    </body></html>
    """)


# ─── Start bot ─────────────────────────────────────────────────────────────────

@app.post("/start_bot")
async def start_bot(request: Request) -> JSONResponse:
    """
    Create a Daily.co room, generate tokens for both the human client and the
    bot, then spawn the bot pipeline in the background.
    """
    if len(active_sessions) >= MAX_CONCURRENT_BOTS:
        logger.warning(f"Max concurrent bots reached ({len(active_sessions)}/{MAX_CONCURRENT_BOTS})")
        raise HTTPException(status_code=503, detail="Max concurrent bots reached")

    room_name = f"pipecat-{uuid.uuid4().hex[:8]}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. Create room
            room_url = await _create_daily_room(client, room_name)
            logger.info(f"[{room_name}] ✅ Room created: {room_url}")

            # 2. Client token (participant)
            client_token = await _create_daily_token(
                client, room_name, is_owner=False, user_name="User"
            )
            if not client_token:
                raise HTTPException(status_code=500, detail="Failed to generate client token")
            logger.info(f"[{room_name}] 🔑 Client token generated")

        # 3. Track session
        active_sessions[room_name] = {
            "status": "created",
            "room_url": room_url,
            "created_at": asyncio.get_event_loop().time(),
        }

        # 4. Spawn bot (generates its own owner token)
        asyncio.create_task(spawn_bot_for_room(room_name, room_url))
        logger.info(f"[{room_name}] 🚀 Bot spawning — returning room info to client")

        return JSONResponse({
            "status": "success",
            "session_id": room_name,
            "room_url": room_url,
            "room_token": client_token,
            "message": "Join the room with Daily.co SDK. Bot will join automatically.",
        })

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"❌ Failed to create bot session: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start bot: {exc}")


async def spawn_bot_for_room(session_id: str, room_url: str) -> None:
    """Generate a bot owner token and run the Pipecat pipeline."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            bot_token = await _create_daily_token(
                client, session_id, is_owner=True, user_name="Pipecat Agent"
            )
        if bot_token:
            logger.info(f"[{session_id}] 🔑 Bot token generated")
        else:
            logger.warning(f"[{session_id}] ⚠️ Could not generate bot token — joining without owner privileges")

        transport = DailyTransport(
            room_url=room_url,
            token=bot_token,
            bot_name="Pipecat Agent",
            params=DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
        )

        active_sessions[session_id]["status"] = "running"
        logger.info(f"[{session_id}] 🤖 Bot joining room...")
        await bot(transport)
        logger.info(f"[{session_id}] ✅ Bot session completed")

    except Exception as exc:
        logger.error(f"[{session_id}] ❌ Bot spawn failed: {type(exc).__name__}: {exc}", exc_info=True)
    finally:
        active_sessions.pop(session_id, None)
        logger.info(f"[{session_id}] 🧹 Session cleaned up")


# ─── Stop bot ──────────────────────────────────────────────────────────────────

@app.post("/stop_bot/{session_id}")
async def stop_bot(session_id: str) -> JSONResponse:
    """Stop a bot session by session_id."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    active_sessions.pop(session_id, None)
    logger.info(f"[{session_id}] 🛑 Session stopped via API")
    return JSONResponse({"status": "success", "session_id": session_id})


# ─── Daily.co webhook (optional) ──────────────────────────────────────────────

@app.post("/webhook")
async def webhook_handler(request: Request):
    """Receive Daily.co room events (participant-joined, participant-left, etc.)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = body.get("event", "unknown")
    session_id = body.get("room_name", "unknown")
    logger.info(f"[{session_id}] Daily.co webhook: {event_type}")

    if event_type == "participant-joined" and session_id in active_sessions:
        active_sessions[session_id]["status"] = "running"
    elif event_type == "participant-left":
        active_sessions.pop(session_id, None)

    return {"status": "received"}


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import socket

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

    parser = argparse.ArgumentParser(description="Pipecat Bot Runner (Daily.co)")
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