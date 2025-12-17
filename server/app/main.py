"""
Roblox RL Gym Server

A reinforcement learning environment server for training models to edit Roblox experiences.
Receives delta strings, applies them via DataModelDeltaService, and captures screenshots.
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api_keys import api_key_manager
from app.config_manager import config as app_config
from app.endpoints import delta_results, evaluate, events
from app.utils.fflag_manager import managed_fflags
from app.utils.plugin_manager import managed_plugin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Sets up infrastructure for plugin communication. The evaluate endpoint
    manages its own temporary Studio instances, so no persistent Studio is
    started here. The infrastructure (request_queue, active_requests) is
    shared and used by both SSE and delta_result endpoints.

    Plugin and FFlags are installed once at startup and reused across all
    Studio instances for better performance.
    """
    # Initialize authentication
    if app_config.enable_auth:
        api_key_manager.load()
    else:
        logger.warning("Authentication is disabled. All endpoints are unprotected.")

    # Install plugin and fflags once, reuse across all Studio instances
    async with (
        managed_fflags() as fflag_manager,
        managed_plugin() as plugin_manager,
    ):
        # Set up shared infrastructure for plugin communication
        app.state.studio_manager = None
        app.state.request_queue = asyncio.Queue()
        app.state.active_requests = {}
        app.state.rate_limiter = defaultdict(list)
        app.state.accepting_requests = True
        app.state.evaluate_lock = asyncio.Lock()

        # Cached managers for reuse across evaluations
        app.state.plugin_manager = plugin_manager
        app.state.fflag_manager = fflag_manager

        logger.info("Server started - ready to accept /evaluate requests")

        yield

        logger.info("Shutting down server...")

    # Stop accepting new requests
    app.state.accepting_requests = False
    logger.info("Stopped accepting new requests")

    # Wait for active requests to complete (with timeout)
    max_wait = app_config.shutdown_timeout
    wait_interval = 0.5
    elapsed = 0.0

    while app.state.active_requests and elapsed < max_wait:
        active_count = len(app.state.active_requests)
        logger.info(f"Waiting for {active_count} active request(s) to complete...")
        await asyncio.sleep(wait_interval)
        elapsed += wait_interval

    if app.state.active_requests:
        logger.warning(
            f"Force stopping with {len(app.state.active_requests)} request(s) still active"
        )

    logger.info("Cleanup complete")


app = FastAPI(
    title="Roblox RL Gym",
    description="Reinforcement learning environment for training models to edit Roblox experiences",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=app_config.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include endpoints
app.include_router(evaluate.router)
app.include_router(events.router)
app.include_router(delta_results.router)


@app.get("/health")
async def health_check(request: Request):
    """Health check endpoint."""
    studio_manager = request.app.state.studio_manager

    # If a Studio is currently active, include its health status
    if studio_manager is not None:
        health_status = studio_manager.is_healthy()
        all_healthy = all(health_status.values())
        return {
            "status": "healthy" if all_healthy else "degraded",
            "studio_active": True,
            **health_status,
        }

    return {"status": "healthy", "studio_active": False}
