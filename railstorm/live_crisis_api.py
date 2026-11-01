"""Loopback-only operator API for bounded synthetic live sessions."""

import os
import socket
import struct
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from railstorm.domain import DomainError
from railstorm.live_crisis import SCENARIOS, LiveCrisis


class Initialize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    seed: int = Field(default=20261030, ge=0, le=2147483647)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ADVANCE", "OUTAGE", "LOSE_ACK", "RESTORE", "RETRY", "RECOVER", "AUDIT", "REPLAY"]


def bridge_gateway():
    """Exact kernel route gateway; only used with explicit local-Compose opt-in."""
    try:
        for row in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = row.split()
            if fields[1] == "00000000" and int(fields[3], 16) & 2:
                return socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
    except (OSError, ValueError, IndexError):
        pass
    return None


def register(app, store):
    service = LiveCrisis(store)

    def local(request):
        gateway_client = (
            os.environ.get("RAILSTORM_TRUST_LOCAL_COMPOSE_GATEWAY") == "1"
            and request.client
            and request.client.host == bridge_gateway()
            and request.url.hostname in ("127.0.0.1", "localhost", "::1")
        )
        if not request.client or (
            request.client.host not in ("127.0.0.1", "::1", "testclient") and not gateway_client
        ):
            raise DomainError("LOCAL_OPERATOR_ONLY")
        if request.headers.get("origin"):
            origin = urlsplit(request.headers["origin"])
            if origin.hostname not in ("localhost", "127.0.0.1", "::1") or origin.scheme not in (
                "http",
                "https",
            ):
                raise DomainError("LOCAL_OPERATOR_ORIGIN_REQUIRED")

    @app.get("/api/v1/live-crisis/scenarios")
    def scenarios():
        return SCENARIOS

    @app.post("/api/v1/live-crisis/sessions")
    def initialize(body: Initialize, request: Request):
        local(request)
        return service.initialize(body.scenario, body.seed)

    @app.get("/api/v1/live-crisis/sessions/{run}")
    def inspect(run: str):
        return service.inspect(run)

    @app.post("/api/v1/live-crisis/sessions/{run}/actions")
    def action(run: str, body: Action, request: Request):
        local(request)
        return service.action(run, body.action)
