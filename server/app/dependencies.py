"""Dependency injection for better testability and modularity"""

from typing import Annotated

from fastapi import Depends, Request

from app.utils.studio_manager import StudioManager


def get_studio_manager(request: Request) -> StudioManager:
    """Get StudioManager instance from app state"""
    if not hasattr(request.app.state, "studio_manager"):
        raise RuntimeError("StudioManager not initialized")
    if request.app.state.studio_manager is None:
        raise RuntimeError("StudioManager not currently active")
    return request.app.state.studio_manager


def get_request_queue(request: Request):
    """Get request queue from app state"""
    if not hasattr(request.app.state, "request_queue"):
        raise RuntimeError("Request queue not initialized")
    return request.app.state.request_queue


def get_active_requests(request: Request) -> dict:
    """Get active requests dict from app state"""
    if not hasattr(request.app.state, "active_requests"):
        raise RuntimeError("Active requests not initialized")
    return request.app.state.active_requests


def get_rate_limiter(request: Request) -> dict:
    """Get rate limiter dict from app state"""
    if not hasattr(request.app.state, "rate_limiter"):
        from collections import defaultdict

        return defaultdict(list)
    return request.app.state.rate_limiter


def get_accepting_requests(request: Request) -> bool:
    """Check if server is accepting new requests"""
    if not hasattr(request.app.state, "accepting_requests"):
        return False
    return request.app.state.accepting_requests


# Type annotations for dependency injection
StudioManagerDep = Annotated[StudioManager, Depends(get_studio_manager)]
RequestQueueDep = Annotated[object, Depends(get_request_queue)]
ActiveRequestsDep = Annotated[dict, Depends(get_active_requests)]
RateLimiterDep = Annotated[dict, Depends(get_rate_limiter)]
AcceptingRequestsDep = Annotated[bool, Depends(get_accepting_requests)]
