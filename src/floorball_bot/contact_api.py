from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json

import asyncpg
from aiohttp import web
from pydantic import ValidationError

from floorball_bot.contact_requests import (
    ContactRequest,
    ContactRequestConflict,
    ContactRequestRateLimited,
    accept_contact_request,
)

POOL_KEY = web.AppKey("contact_pool", asyncpg.Pool)
SECRET_KEY = web.AppKey("contact_fingerprint_secret", bytes)
ORIGINS_KEY = web.AppKey("contact_allowed_origins", frozenset)


def _client_address(request: web.Request) -> str:
    remote = request.remote or "unknown"
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        return "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if remote_ip.is_loopback and forwarded:
        candidate = forwarded.split(",", 1)[0].strip()
        try:
            return ipaddress.ip_address(candidate).compressed
        except ValueError:
            return remote_ip.compressed
    return remote_ip.compressed


def _fingerprint(request: web.Request, secret: bytes) -> str:
    return hmac.new(secret, _client_address(request).encode(), hashlib.sha256).hexdigest()


def _cors_response(
    request: web.Request,
    payload: dict,
    *,
    status: int,
) -> web.Response:
    response = web.json_response(payload, status=status)
    origin = request.headers.get("Origin")
    if origin in request.app[ORIGINS_KEY]:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


async def _options(request: web.Request) -> web.Response:
    origin = request.headers.get("Origin")
    if origin not in request.app[ORIGINS_KEY]:
        return web.json_response({"error": "origin_forbidden"}, status=403)
    response = web.Response(status=204)
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers["Vary"] = "Origin"
    return response


async def _accept(request: web.Request) -> web.Response:
    if request.headers.get("Origin") not in request.app[ORIGINS_KEY]:
        return web.json_response({"error": "origin_forbidden"}, status=403)
    try:
        payload = await request.json(loads=json.loads)
        contact_request = ContactRequest.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest):
        return _cors_response(request, {"error": "invalid_request"}, status=422)
    try:
        result = await accept_contact_request(
            request.app[POOL_KEY],
            request=contact_request,
            client_fingerprint=_fingerprint(request, request.app[SECRET_KEY]),
        )
    except ContactRequestConflict:
        return _cors_response(request, {"error": "request_id_conflict"}, status=409)
    except ContactRequestRateLimited:
        response = _cors_response(request, {"error": "rate_limited"}, status=429)
        response.headers["Retry-After"] = "600"
        return response
    return _cors_response(
        request,
        {"requestId": str(result.request_id), "status": result.status},
        status=202,
    )


async def _health(request: web.Request) -> web.Response:
    try:
        await request.app[POOL_KEY].execute("SELECT 1")
    except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError):
        return web.json_response({"status": "unavailable"}, status=503)
    return web.json_response({"status": "ok"})


def create_contact_app(
    pool: asyncpg.Pool,
    *,
    fingerprint_secret: bytes,
    allowed_origins: tuple[str, ...],
) -> web.Application:
    if len(fingerprint_secret) < 32:
        raise ValueError("contact fingerprint secret must contain at least 32 bytes")
    normalized_origins = tuple(origin.rstrip("/") for origin in allowed_origins if origin)
    if not normalized_origins:
        raise ValueError("at least one contact API origin is required")
    app = web.Application(client_max_size=16 * 1024)
    app[POOL_KEY] = pool
    app[SECRET_KEY] = fingerprint_secret
    app[ORIGINS_KEY] = frozenset(normalized_origins)
    app.router.add_get("/healthz", _health)
    app.router.add_options("/api/contact/v1/requests", _options)
    app.router.add_post("/api/contact/v1/requests", _accept)
    return app
