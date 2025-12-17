"""
Evaluate endpoint for RL training.

Accepts a place file and list of deltas, captures before/after screenshots.
"""

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.auth import ExternalAuthDep, InternalAuthDep
from app.config_manager import config as app_config
from app.utils.screenshot_capture import capture_studio_screenshot
from app.utils.studio_manager import managed_studio

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
    place_file: UploadFile = File(..., description="The .rbxl place file"),
    deltas: str = Form(..., description="JSON array of delta strings"),
) -> EvaluateResponse:
    """
    Evaluate a list of deltas against a place file.

    For each delta, opens the place in Studio, captures before/after screenshots.
    The "before" screenshot is captured once (identical for all deltas since
    each starts from the same place file). Each delta is evaluated independently.
    """
    request_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()

    # Parse deltas JSON
    try:
        delta_list = json.loads(deltas)
        if not isinstance(delta_list, list):
            raise ValueError("deltas must be a JSON array")
        if not delta_list:
            raise ValueError("deltas array cannot be empty")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in deltas: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Validate file extension
    if not place_file.filename or not place_file.filename.endswith(".rbxl"):
        raise HTTPException(status_code=400, detail="place_file must be a .rbxl file")

    # Acquire lock to prevent concurrent evaluations
    async with request.app.state.evaluate_lock:
        return await _run_evaluation(
            request, request_id, timestamp, delta_list, place_file
        )


async def _run_evaluation(
    request: Request,
    request_id: str,
    timestamp: str,
    delta_list: list,
    place_file: UploadFile,
) -> EvaluateResponse:
    """Internal function that runs the actual evaluation logic."""
    # Save uploaded file to temp location
    temp_dir = tempfile.mkdtemp(prefix="roblox_eval_")
    temp_place_path = Path(temp_dir) / "place.rbxl"

    try:
        # Write uploaded file to temp location
        content = await place_file.read()
        temp_place_path.write_bytes(content)
        logger.info(f"Saved place file to {temp_place_path} ({len(content)} bytes)")

        before_screenshot = None
        results: list[DeltaEvalResult] = []

        # Get cached managers from app state
        plugin_manager = request.app.state.plugin_manager
        fflag_manager = request.app.state.fflag_manager

        # Step 1: Capture baseline "before" screenshot
        logger.info(f"[{request_id}] Capturing baseline screenshot...")

        mgr = managed_studio(temp_place_path, plugin_manager, fflag_manager)
        async with mgr as studio_manager:
            # Temporarily set app state so SSE endpoint can find this manager
            request.app.state.studio_manager = studio_manager

            # Wait for plugin to connect
            if not await wait_for_plugin_connection(studio_manager):
                return EvaluateResponse(
                    request_id=request_id,
                    success=False,
                    error="Plugin did not connect for baseline",
                    timestamp=timestamp,
                )

            # Small delay to ensure rendering is complete
            await asyncio.sleep(0.5)

            # Capture baseline screenshot
            screenshot, screenshot_error = capture_studio_screenshot()
            if screenshot_error:
                return EvaluateResponse(
                    request_id=request_id,
                    success=False,
                    error=f"Baseline screenshot failed: {screenshot_error}",
                    timestamp=timestamp,
                )

            before_screenshot = screenshot
            logger.info(f"[{request_id}] Baseline screenshot captured")

        # Step 2: Process each delta
        for idx, delta in enumerate(delta_list):
            delta_request_id = f"{request_id}_delta_{idx}"
            delta_count = len(delta_list)
            logger.info(f"[{request_id}] Processing delta {idx+1}/{delta_count}")

            try:
                mgr = managed_studio(temp_place_path, plugin_manager, fflag_manager)
                async with mgr as studio_manager:
                    request.app.state.studio_manager = studio_manager

                    # Wait for plugin to connect
                    if not await wait_for_plugin_connection(studio_manager):
                        results.append(
                            DeltaEvalResult(
                                delta_index=idx,
                                success=False,
                                error="Plugin did not connect",
                            )
                        )
                        continue

                    # Apply the delta
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

                    # Small delay to ensure rendering is complete
                    await asyncio.sleep(0.1)

                    # Capture after screenshot
                    screenshot, screenshot_error = capture_studio_screenshot()

                    results.append(
                        DeltaEvalResult(
                            delta_index=idx,
                            success=True,
                            error=screenshot_error,
                            after_screenshot=screenshot,
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

    except Exception as e:
        logger.error(f"[{request_id}] Evaluate failed: {e}")
        return EvaluateResponse(
            request_id=request_id,
            success=False,
            error=str(e),
            timestamp=timestamp,
        )

    finally:
        # Clear studio manager from app state
        request.app.state.studio_manager = None

        # Cleanup temp directory and all contents
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.debug(f"Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp files: {e}")
