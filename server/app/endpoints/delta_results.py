"""
Endpoint for the plugin to report delta application and reset results.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.auth import InternalAuthDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/_delta_result")
async def submit_delta_result(request: Request, _auth: InternalAuthDep):
    """
    Receive the result of a delta application or reset from the plugin.

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
    if request_info and "future" in request_info:
        if not request_info["future"].done():
            request_info["future"].set_result({
                "success": success,
                "error": error,
            })
        else:
            logger.warning(f"Request {request_id} future already completed")

    return {"status": "accepted", "request_id": request_id}

