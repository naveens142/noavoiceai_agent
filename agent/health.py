"""
Health check endpoint for the Pipecat agent.

Provides a simple /health endpoint to verify agent is running.
Patches uvicorn.run to add the endpoint when the FastAPI app is created.
"""

import time
from fastapi.responses import JSONResponse
from agent.utils.logger import get_logger

logger = get_logger(__name__)

_startup_time = time.time()
_app_config = None


def configure_health(app_config):
    """Set the app config for health endpoint."""
    global _app_config
    _app_config = app_config


def patch_uvicorn_with_health():
    """
    Patch uvicorn.run to add health endpoint to the FastAPI app before starting.
    Call this BEFORE pipecat_main() is invoked.
    """
    import uvicorn
    from pipecat.runner import run as runner_module
    
    # Store original uvicorn.run
    original_uvicorn_run = uvicorn.run
    
    def run_with_health(app, *args, **kwargs):
        """Wrapper for uvicorn.run that adds health endpoint to the FastAPI app."""
        logger.info("Adding health endpoint to FastAPI app")
        
        @app.get("/health")
        async def health():
            """
            Health check endpoint for the agent.
            Verify agent is reachable and running.
            """
            return JSONResponse({
                "status": "ok",
                "uptime_seconds": round(time.time() - _startup_time),
                "agent": _app_config.agent_name if _app_config else "initializing",
            })
        
        logger.info("Health endpoint registered at GET /health")
        
        # Call original uvicorn.run with the modified app
        return original_uvicorn_run(app, *args, **kwargs)
    
    # Patch uvicorn.run globally
    uvicorn.run = run_with_health
    logger.info("uvicorn.run patched for health endpoint")
