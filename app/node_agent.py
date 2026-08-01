"""Standalone ping-test agent deployed to remote 'node' servers by the main
Channel Guard panel. Deliberately has ZERO imports from the `app` package -
it is uploaded and run on a third-party server on its own, independent of
our codebase layout. Keep it dependency-light (aiohttp + stdlib only).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

VERSION = "3"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


async def tcp_connect_ping(host: str, port: int, timeout: float = 3.0) -> float | None:
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except Exception:
        return None
    elapsed_ms = (time.perf_counter() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return elapsed_ms


def make_app(cfg: dict) -> web.Application:
    token = cfg["api_token"]

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"status": False, "msg": "unauthorized"}, status=401)
        return await handler(request)

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": VERSION})

    async def handle_test(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            targets = data["targets"]
        except Exception:
            return web.json_response({"status": False, "msg": "bad request"}, status=400)

        async def probe(t: dict) -> dict:
            ping_ms = await tcp_connect_ping(t["host"], int(t["port"]))
            return {"host": t["host"], "port": t["port"], "ping_ms": ping_ms}

        results = await asyncio.gather(*(probe(t) for t in targets))
        return web.json_response({"status": True, "results": results})

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/health", handle_health)
    app.router.add_post("/test", handle_test)
    return app


if __name__ == "__main__":
    cfg = load_config()
    web.run_app(make_app(cfg), host="0.0.0.0", port=int(cfg["api_port"]))
