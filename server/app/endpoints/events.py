"""
Server-Sent Events endpoint for plugin communication.

Handles streaming delta_apply and reset events to the Roblox Studio plugin.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.auth import InternalAuthDep
from app.dependencies import StudioManagerDep

logger = logging.getLogger(__name__)

router = APIRouter()


async def event_generator(
    request: Request,
    studio_manager: StudioManagerDep,
) -> AsyncGenerator:
    """
    Generate SSE events for the plugin.

    Sends delta_apply and reset events from the request queue.
    """
    logger.info("Client connected to SSE endpoint")
    current_request_data = None

    studio_manager._plugin_connections.add(request)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("Client disconnected from SSE")
                studio_manager._plugin_connections.remove(request)
                # Put request data back if we have any that wasn't fully sent
                if current_request_data is not None:
                    logger.warning(
                        f"Client disconnected during request {current_request_data['request_id']}, re-queueing"
                    )
                    await request.app.state.request_queue.put(current_request_data)
                break

            try:
                # Wait for a request from the queue
                request_data = await asyncio.wait_for(
                    request.app.state.request_queue.get(), timeout=15.0
                )
                current_request_data = request_data

                request_id = request_data["request_id"]
                request_type = request_data["type"]

                logger.info(f"Sending {request_type} request {request_id} to plugin")

                if request_type == "delta_apply":
                    # Send delta_apply event with the delta string
                    yield {
                        "event": "delta_apply",
                        "data": json.dumps({
                            "request_id": request_id,
                            "delta": request_data["delta"],
                        }),
                    }
                elif request_type == "reset":
                    # Send reset event
                    yield {
                        "event": "reset",
                        "data": json.dumps({
                            "request_id": request_id,
                        }),
                    }
                else:
                    logger.warning(f"Unknown request type: {request_type}")

                # Successfully sent the event
                current_request_data = None

            except TimeoutError:
                # Timed out while waiting for a request
                # Send a ping to keep the connection alive
                continue

    except asyncio.CancelledError:
        studio_manager._plugin_connections.remove(request)
        logger.info("SSE connection cancelled")
        # Put request data back if we have any that wasn't fully sent
        if current_request_data is not None:
            logger.warning(
                f"SSE cancelled during request {current_request_data['request_id']}, re-queueing"
            )
            await request.app.state.request_queue.put(current_request_data)
        raise
    except Exception as e:
        studio_manager._plugin_connections.remove(request)
        logger.error(f"Error in SSE stream: {e}")
        # Put request data back if we have any that wasn't fully sent
        if current_request_data is not None:
            logger.warning(
                f"Error during request {current_request_data['request_id']}: {e}, re-queueing"
            )
            await request.app.state.request_queue.put(current_request_data)
        raise


@router.get("/_events")
async def events_stream(
    request: Request,
    _auth: InternalAuthDep,
    studio_manager: StudioManagerDep,
):
    """SSE endpoint for plugin to receive delta_apply and reset events."""
    return EventSourceResponse(event_generator(request, studio_manager))
