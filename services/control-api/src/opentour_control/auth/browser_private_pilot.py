"""Local private browser identity route inventory. Imports do not mount routes."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

from .browser_private import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES


class PrivateBrowserPilotPathProfile(Enum):
    LOOPBACK = "loopback"
    DEV = "dev"


@dataclass(frozen=True, slots=True)
class PrivateBrowserPilotRoute:
    path: str
    operation: str
    method: Literal["POST"] = field(default="POST", init=False)
    max_request_bytes: int = field(default=MAX_REQUEST_BYTES, init=False)
    max_response_bytes: int = field(default=MAX_RESPONSE_BYTES, init=False)


PRIVATE_BROWSER_PILOT_ROUTES: Final = (
    PrivateBrowserPilotRoute("/internal/browser-identity/v1/flows/create", "create_flow"),
    PrivateBrowserPilotRoute("/internal/browser-identity/v1/flows/consume", "consume_flow"),
    PrivateBrowserPilotRoute("/internal/browser-identity/v1/sessions/admit", "admit"),
    PrivateBrowserPilotRoute("/internal/browser-identity/v1/sessions/current", "current"),
    PrivateBrowserPilotRoute("/internal/browser-identity/v1/sessions/end", "end"),
)

# The dev backend accepts only these landed targets. The external methods above stay unchanged.
PRIVATE_BROWSER_DEV_ROUTES: Final = (
    PrivateBrowserPilotRoute("/dev/internal/browser-identity/v1/flows/create", "create_flow"),
    PrivateBrowserPilotRoute("/dev/internal/browser-identity/v1/flows/consume", "consume_flow"),
    PrivateBrowserPilotRoute("/dev/internal/browser-identity/v1/sessions/admit", "admit"),
    PrivateBrowserPilotRoute("/dev/internal/browser-identity/v1/sessions/current", "current"),
    PrivateBrowserPilotRoute("/dev/internal/browser-identity/v1/sessions/end", "end"),
)


def lookup_private_browser_pilot_route(
    method: str,
    raw_path: str,
    *,
    path_profile: PrivateBrowserPilotPathProfile = PrivateBrowserPilotPathProfile.LOOPBACK,
) -> PrivateBrowserPilotRoute | None:
    """Match the verb and raw request target without decoding or rewriting them."""
    if path_profile is PrivateBrowserPilotPathProfile.LOOPBACK:
        routes = PRIVATE_BROWSER_PILOT_ROUTES
    elif path_profile is PrivateBrowserPilotPathProfile.DEV:
        routes = PRIVATE_BROWSER_DEV_ROUTES
    else:
        return None
    return next(
        (route for route in routes if route.method == method and route.path == raw_path),
        None,
    )
