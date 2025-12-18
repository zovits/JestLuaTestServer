"""
Endpoint for the plugin to report delta application and reset results.

The screenshot is captured here (before responding) so the plugin keeps the delta
applied until we've captured the visual state. The server then waits for the
plugin to signal undo completion before sending the next delta.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app.auth import InternalAuthDep
from app.utils.screenshot_capture import capture_studio_screenshot

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/_delta_result")
async def submit_delta_result(request: Request, _auth: InternalAuthDep):
    """
    Receive the result of a delta application from the plugin.

    After receiving this POST, the server captures a screenshot while the delta
    is still applied. The plugin waits for our response before undoing the delta,
    then calls /_undo_complete when done.

    Expected body:
    {
        "request_id": "uuid",
        "success": true/false,
        "error": "error message if failed" (optional)
    }
    """
    body = await request.json()
    request_id = body.get("request_id")
    success = body.get("success", False)
    error = body.get("error")

    logger.info(f"Plugin posted result for request {request_id}: success={success}")

    if request_id not in request.app.state.active_requests:
        logger.warning(f"Received result for unknown request {request_id}")
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    request_info = request.app.state.active_requests.get(request_id)

    screenshot = None
    screenshot_error = None

    if success and request_info and request_info.get("type") == "delta_apply":
        # Small delay to ensure rendering is complete before screenshotting
        await asyncio.sleep(0.1)

        logger.info(f"Capturing screenshot for request {request_id}")
        screenshot, screenshot_error = capture_studio_screenshot()

        if screenshot_error:
            logger.warning(
                f"Screenshot capture failed for {request_id}: {screenshot_error}"
            )
        else:
            logger.info(f"Screenshot captured for request {request_id}")

    # Store screenshot data for when undo completes
    if request_info:
        request_info["screenshot"] = screenshot
        request_info["screenshot_error"] = screenshot_error
        request_info["apply_success"] = success
        request_info["apply_error"] = error

    return {"status": "accepted", "request_id": request_id}


@router.post("/_undo_complete")
async def undo_complete(request: Request, _auth: InternalAuthDep):
    """
    Receive notification that the plugin has finished undoing a delta.

    This signals the server to proceed with the next delta.

    Expected body:
    {
        "request_id": "uuid",
        "success": true/false,
        "error": "error message if failed" (optional)
    }
    """
    body = await request.json()
    request_id = body.get("request_id")
    success = body.get("success", False)
    error = body.get("error")

    logger.info(f"Plugin completed undo for request {request_id}: success={success}")

    if request_id not in request.app.state.active_requests:
        logger.warning(f"Received undo_complete for unknown request {request_id}")
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    request_info = request.app.state.active_requests.get(request_id)

    if request_info and "future" in request_info:
        if not request_info["future"].done():
            # Resolve the future with screenshot data captured earlier
            request_info["future"].set_result(
                {
                    "success": request_info.get("apply_success", False),
                    "error": request_info.get("apply_error")
                    or (error if not success else None),
                    "screenshot": request_info.get("screenshot"),
                    "screenshot_error": request_info.get("screenshot_error"),
                    "undo_success": success,
                    "undo_error": error,
                }
            )
        else:
            logger.warning(f"Request {request_id} future already completed")

    return {"status": "accepted", "request_id": request_id}
