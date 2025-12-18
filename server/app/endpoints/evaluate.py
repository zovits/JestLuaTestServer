"""
Evaluate endpoint for RL training.

Launches Studio once, captures a "before" screenshot, then iterates through deltas.
For each delta, the plugin applies it, the server screenshots, and the plugin undoes it.
"""

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import ExternalAuthDep, InternalAuthDep
from app.config_manager import config as app_config
from app.utils.screenshot_capture import capture_studio_screenshot
from app.utils.studio_manager import StudioManager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/_heartbeat")
async def heartbeat(
    request: Request,
    _auth: InternalAuthDep,
):
    """Receive heartbeat from plugin to indicate it's alive."""
    studio_manager = request.app.state.studio_manager
    if studio_manager is not None:
        studio_manager.update_heartbeat()
    return {"status": "ok"}


class EvaluateRequest(BaseModel):
    """Request body for the /evaluate endpoint."""

    place_id: int
    universe_id: int
    deltas: list[str]


class DeltaEvalResult(BaseModel):
    """Result of evaluating a single delta."""

    delta_index: int
    success: bool
    error: str | None = None
    after_screenshot: str | None = None


class EvaluateResponse(BaseModel):
    """Response from the /evaluate endpoint."""

    request_id: str
    success: bool
    error: str | None = None
    before_screenshot: str | None = None
    results: list[DeltaEvalResult] = []
    timestamp: str


async def wait_for_plugin_connection(
    studio_manager,
    timeout: float = 30.0,
) -> bool:
    """Wait for the plugin to connect to the SSE endpoint."""
    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < timeout:
        if len(studio_manager._plugin_connections) > 0:
            logger.info("Plugin connected")
            return True
        await asyncio.sleep(0.25)

    logger.warning(f"Plugin did not connect within {timeout} seconds")
    return False


async def apply_delta_and_wait(
    request: Request,
    studio_manager,
    delta: str,
    request_id: str,
    timeout: float = 30.0,
) -> dict:
    """Apply a delta via the plugin and wait for the result."""
    result_future: asyncio.Future = asyncio.Future()

    # Register the request so delta_result endpoint can resolve it
    request.app.state.active_requests[request_id] = {
        "type": "delta_apply",
        "delta": delta,
        "future": result_future,
    }

    try:
        # Queue the delta for the plugin
        await request.app.state.request_queue.put(
            {
                "request_id": request_id,
                "type": "delta_apply",
                "delta": delta,
            }
        )

        # Wait for the plugin to apply the delta
        outcome = await asyncio.wait_for(result_future, timeout=timeout)
        return outcome

    except TimeoutError:
        error_msg = f"Timed out after {timeout} seconds"
        logger.error(f"Delta application {error_msg}")
        return {"success": False, "error": error_msg}
    finally:
        request.app.state.active_requests.pop(request_id, None)


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(
    request: Request,
    _auth: ExternalAuthDep,
    body: EvaluateRequest,
) -> EvaluateResponse:
    """
    Evaluate a list of deltas against a Roblox place.

    Launches Studio once, captures a "before" screenshot, then for each delta:
    1. Sends delta to plugin via SSE
    2. Plugin applies delta and POSTs back
    3. Server captures "after" screenshot before responding
    4. Plugin undoes delta after receiving response
    """
    request_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()

    # Validate deltas list
    if not body.deltas:
        raise HTTPException(status_code=400, detail="deltas array cannot be empty")

    # Validate user_id is configured
    if app_config.user_id is None:
        raise HTTPException(
            status_code=500,
            detail="user_id not configured. Set ROBLOX_RL_GYM_USER_ID environment variable.",
        )

    # Acquire lock to prevent concurrent evaluations
    async with request.app.state.evaluate_lock:
        return await _run_evaluation(
            request,
            request_id,
            timestamp,
            body.deltas,
            body.place_id,
            body.universe_id,
        )


async def _run_evaluation(
    request: Request,
    request_id: str,
    timestamp: str,
    delta_list: list,
    place_id: int,
    universe_id: int,
) -> EvaluateResponse:
    """
    Launch Studio once and iterate through all deltas.

    The plugin applies each delta, waits for our screenshot, then undoes it before
    the next delta is sent. This avoids the overhead of restarting Studio per delta.
    """
    assert app_config.user_id is not None
    user_id = app_config.user_id

    studio_manager = StudioManager(place_id, universe_id, user_id)
    studio_manager.plugin_manager = request.app.state.plugin_manager
    studio_manager.fflag_manager = request.app.state.fflag_manager

    # Set app state before starting so the SSE endpoint can find the manager
    request.app.state.studio_manager = studio_manager

    try:
        logger.info(f"[{request_id}] Starting Studio...")
        async with studio_manager:
            # Wait for plugin to connect
            if not await wait_for_plugin_connection(studio_manager):
                return EvaluateResponse(
                    request_id=request_id,
                    success=False,
                    error="Plugin did not connect",
                    timestamp=timestamp,
                )

            # Capture the "before" screenshot
            await asyncio.sleep(0.5)
            before_screenshot, screenshot_error = capture_studio_screenshot()
            if screenshot_error:
                return EvaluateResponse(
                    request_id=request_id,
                    success=False,
                    error=f"Baseline screenshot failed: {screenshot_error}",
                    timestamp=timestamp,
                )
            logger.info(f"[{request_id}] Baseline screenshot captured")

            # Process each delta (plugin applies, we screenshot, plugin undoes)
            results: list[DeltaEvalResult] = []
            delta_count = len(delta_list)

            for idx, delta in enumerate(delta_list):
                delta_request_id = f"{request_id}_delta_{idx}"
                logger.info(f"[{request_id}] Processing delta {idx + 1}/{delta_count}")

                try:
                    outcome = await apply_delta_and_wait(
                        request=request,
                        studio_manager=studio_manager,
                        delta=delta,
                        request_id=delta_request_id,
                        timeout=app_config.step_timeout,
                    )

                    if not outcome.get("success"):
                        results.append(
                            DeltaEvalResult(
                                delta_index=idx,
                                success=False,
                                error=outcome.get("error", "Unknown error"),
                            )
                        )
                        continue

                    # Screenshot is captured in _delta_result before the plugin undoes
                    results.append(
                        DeltaEvalResult(
                            delta_index=idx,
                            success=True,
                            error=outcome.get("screenshot_error"),
                            after_screenshot=outcome.get("screenshot"),
                        )
                    )
                    logger.info(f"[{request_id}] Delta {idx} completed successfully")

                except Exception as e:
                    logger.error(f"[{request_id}] Delta {idx} error: {e}")
                    results.append(
                        DeltaEvalResult(
                            delta_index=idx,
                            success=False,
                            error=str(e),
                        )
                    )

            return EvaluateResponse(
                request_id=request_id,
                success=True,
                before_screenshot=before_screenshot,
                results=results,
                timestamp=timestamp,
            )

    except RuntimeError as e:
        # Context manager raises RuntimeError if Studio fails to start
        logger.error(f"[{request_id}] Failed to start Studio: {e}")
        return EvaluateResponse(
            request_id=request_id,
            success=False,
            error="Failed to start Roblox Studio",
            timestamp=timestamp,
        )

    except Exception as e:
        logger.error(f"[{request_id}] Evaluate failed: {e}")
        return EvaluateResponse(
            request_id=request_id,
            success=False,
            error=str(e),
            timestamp=timestamp,
        )

    finally:
        request.app.state.studio_manager = None
