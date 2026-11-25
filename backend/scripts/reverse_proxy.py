#!/usr/bin/env python3
"""Lightweight user-space reverse proxy for 197 (LRM-02) where system nginx/caddy is absent.

Forwards incoming HTTP (or TLS HTTPS if --ssl-certfile/--ssl-keyfile provided) traffic
on 0.0.0.0:8080 / 0.0.0.0:8443 to the low-privilege FastAPI backend on 127.0.0.1:8680,
injecting X-Forwarded-Proto and X-Forwarded-For headers and supporting SSE streaming.
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def build_proxy_app(upstream_url: str, forwarded_proto: str) -> Starlette:
    client = httpx.AsyncClient(
        base_url=upstream_url,
        timeout=None,
        follow_redirects=False,
        trust_env=False,
    )

    async def proxy_endpoint(request: Request) -> StreamingResponse:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }
        headers["host"] = request.headers.get("host", "127.0.0.1:8680")
        headers["x-forwarded-proto"] = forwarded_proto
        if request.client:
            headers["x-forwarded-for"] = request.client.host

        body = await request.body()
        req = client.build_request(request.method, path, headers=headers, content=body)
        upstream_resp = await client.send(req, stream=True)

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        resp_headers = [
            (k, v)
            for k, v in upstream_resp.headers.raw
            if k.decode("latin1").lower() not in HOP_BY_HOP
        ]
        response = StreamingResponse(_stream(), status_code=upstream_resp.status_code)
        response.raw_headers = list(resp_headers)
        return response

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(
        routes=[
            Route("/", proxy_endpoint, methods=methods),
            Route("/{path:path}", proxy_endpoint, methods=methods),
        ],
        lifespan=lifespan,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="User-space reverse proxy for LRM Eval.")
    parser.add_argument("--listen-host", default=os.environ.get("LRM_PROXY_HOST", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int, default=int(os.environ.get("LRM_PROXY_PORT", "8080")))
    parser.add_argument("--upstream", default=os.environ.get("LRM_UPSTREAM_URL", "http://127.0.0.1:8680"))
    parser.add_argument("--ssl-certfile", default=os.environ.get("LRM_SSL_CERTFILE"))
    parser.add_argument("--ssl-keyfile", default=os.environ.get("LRM_SSL_KEYFILE"))
    parser.add_argument("--logs-dir", default=os.environ.get("LRM_LOGS_DIR", ""))
    args = parser.parse_args()

    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.logging_setup import build_uvicorn_log_config

    runtime_dir = Path(os.environ.get("LRM_RUNTIME_DIR") or (Path.home() / ".local" / "share" / "lrm-eval"))
    logs_dir = Path(args.logs_dir) if args.logs_dir else (runtime_dir / "state" / "logs")
    log_cfg = build_uvicorn_log_config(logs_dir, "proxy", emit_console=False)

    proto = "https" if (args.ssl_certfile and args.ssl_keyfile) else "http"
    app = build_proxy_app(args.upstream, forwarded_proto=proto)
    uvicorn.run(
        app,
        host=args.listen_host,
        port=args.listen_port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        log_config=log_cfg,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
