"""
Prism — transparent LLM reverse proxy with request/response logging.

Designed by SK. Built by Claude.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

# ── config ───────────────────────────────────────────────────────────────
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "1319"))
LOG_DIR = Path(os.environ.get("LOG_DIR", os.path.join(os.path.dirname(__file__), "logs")))
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "14400"))

# ── logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("prism")

# ── app ──────────────────────────────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

HOP_BY_HOP = frozenset({"host", "transfer-encoding", "connection", "keep-alive"})
STRIP_RESP = frozenset({"content-encoding", "content-length"})  # httpx auto-decompresses


@app.on_event("startup")
async def startup():
    app.state.client = httpx.AsyncClient(
        base_url=BACKEND_URL,
        timeout=httpx.Timeout(BACKEND_TIMEOUT, connect=10.0),
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Prism ready  :%d -> %s  logs=%s", LISTEN_PORT, BACKEND_URL, LOG_DIR)


@app.on_event("shutdown")
async def shutdown():
    await app.state.client.aclose()


# ── helpers ──────────────────────────────────────────────────────────────
def _rid() -> str:
    """Short unique request id."""
    return uuid.uuid4().hex[:12]


def _fwd_headers(raw: dict) -> dict:
    """Strip hop-by-hop headers before forwarding."""
    return {k: v for k, v in raw.items() if k.lower() not in HOP_BY_HOP}


def _resp_headers(raw: httpx.Headers, *, strip_encoding: bool = False) -> dict:
    """Clean response headers (strip hop-by-hop, optionally encoding)."""
    skip = HOP_BY_HOP | STRIP_RESP if strip_encoding else HOP_BY_HOP
    return {k: v for k, v in raw.items() if k.lower() not in skip}


def _parse_json(raw: bytes) -> dict | None:
    """Best-effort JSON parse, returns None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _tokens(body: dict | None) -> dict | None:
    """Extract token counts from an OpenAI/Anthropic response body."""
    if not body or "usage" not in body:
        return None
    u = body["usage"]
    p = u.get("prompt_tokens") or u.get("input_tokens")
    c = u.get("completion_tokens") or u.get("output_tokens")
    if p is None and c is None:
        return None
    return {"prompt": p, "completion": c, "total": (p or 0) + (c or 0)}


def _tokens_from_timings(t: dict | None) -> dict | None:
    """Extract token counts from llama.cpp timings dict."""
    if not t:
        return None
    p, c = t.get("prompt_n", 0), t.get("predicted_n", 0)
    return {"prompt": p, "completion": c, "total": p + c}


def _parse_sse(chunks: list[bytes]) -> tuple[str, dict | None, str | None]:
    """Reassemble streamed content, extract tokens + model from SSE chunks.

    Handles OpenAI (choices[].delta.content, timings) and
    Anthropic (content_block_delta, message_start/message_delta) formats.
    """
    parts: list[str] = []
    timings = None
    model = None
    inp_tok = None
    out_tok = None

    for line in b"".join(chunks).decode(errors="replace").split("\n"):
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        # OpenAI
        if "timings" in obj:
            timings = obj["timings"]
        if "model" in obj and not model:
            model = obj["model"]
        for ch in obj.get("choices") or []:
            if ch.get("delta", {}).get("content"):
                parts.append(ch["delta"]["content"])

        # Anthropic
        evt = obj.get("type")
        if evt == "message_start":
            msg = obj.get("message", {})
            model = model or msg.get("model")
            inp_tok = msg.get("usage", {}).get("input_tokens", inp_tok)
        elif evt == "content_block_delta":
            txt = obj.get("delta", {}).get("text")
            if txt:
                parts.append(txt)
        elif evt == "message_delta":
            out_tok = obj.get("usage", {}).get("output_tokens", out_tok)

    tokens = None
    if timings:
        tokens = timings
    elif inp_tok is not None or out_tok is not None:
        tokens = {"input_tokens": inp_tok, "output_tokens": out_tok}

    return "".join(parts), tokens, model


async def _save(record: dict) -> None:
    """Write log record to disk. Never blocks the event loop."""
    try:
        day_dir = LOG_DIR / record["timestamp"][:10]
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{time.time():.3f}_{record['id']}.json"
        data = json.dumps(record, ensure_ascii=False, default=str, indent=2)
        await asyncio.to_thread(path.write_text, data)
    except Exception:
        log.exception("Failed to save log %s", record.get("id"))


def _record(
    rid: str, ms: float, req: Request, path: str, body: dict | None,
    *, status: int, resp_headers: dict | None = None, resp_body=None,
    stream: bool = False, model: str | None = None,
    tokens: dict | None = None, error: str | None = None,
) -> dict:
    """Build a structured log record."""
    r = {
        "id": rid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round(ms, 1),
        "request": {"method": req.method, "path": path, "body": body},
        "response": {"status": status, "body": resp_body},
        "meta": {"client_ip": req.client.host if req.client else None},
    }
    if error:
        r["meta"]["error"] = error
    else:
        r["request"]["headers"] = dict(req.headers)
        r["response"]["headers"] = resp_headers
        r["meta"].update(stream=stream, model=model, tokens=tokens)
    return r


def _err502(rid: str, t0: float, req: Request, path: str,
            body: dict | None, exc: Exception) -> Response:
    """Return 502 and log the backend failure."""
    rec = _record(rid, (time.monotonic() - t0) * 1000, req, path, body,
                  status=502, error=str(exc))
    asyncio.create_task(_save(rec))
    log.warning("Backend unavailable: %s", exc)
    return Response(
        json.dumps({"error": "backend unavailable", "detail": str(exc)}),
        status_code=502, media_type="application/json",
    )


# ── stubs ────────────────────────────────────────────────────────────────
@app.api_route("/v1/messages/count_tokens", methods=["POST"])
async def count_tokens_stub(request: Request):
    """Estimate token count — vLLM/llama.cpp don't implement this endpoint."""
    body = _parse_json(await request.body()) or {}
    chars = 0
    for s in body.get("system") or []:
        chars += len(s.get("text", ""))
    for m in body.get("messages") or []:
        c = m.get("content", "")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            chars += sum(len(json.dumps(b, ensure_ascii=False)) for b in c)
    for t in body.get("tools") or []:
        chars += len(json.dumps(t, ensure_ascii=False))
    return Response(
        json.dumps({"input_tokens": max(1, chars // 4)}),
        media_type="application/json",
    )


# ── catch-all ────────────────────────────────────────────────────────────
@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, path: str):
    t0 = time.monotonic()
    rid = _rid()
    client: httpx.AsyncClient = request.app.state.client

    raw_body = await request.body()
    req_json = _parse_json(raw_body)
    is_stream = isinstance(req_json, dict) and req_json.get("stream", False)
    headers = _fwd_headers(dict(request.headers))
    url = f"/{path}" if path else "/"
    params = dict(request.query_params)

    # ── streaming ────────────────────────────────────────────────────
    if is_stream:
        try:
            upstream = client.build_request(
                method=request.method, url=url, headers=headers,
                content=raw_body, params=params,
            )
            backend = await client.send(upstream, stream=True)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return _err502(rid, t0, request, url, req_json, exc)

        buf: list[bytes] = []

        async def generate():
            async for chunk in backend.aiter_bytes():
                buf.append(chunk)
                yield chunk

        async def on_done():
            await backend.aclose()
            ms = (time.monotonic() - t0) * 1000
            content, raw_tok, model = _parse_sse(buf)
            if raw_tok and "prompt_n" in raw_tok:
                tok = _tokens_from_timings(raw_tok)
            elif raw_tok and "input_tokens" in raw_tok:
                tok = _tokens({"usage": raw_tok})
            else:
                tok = None
            rec = _record(rid, ms, request, url, req_json,
                          status=backend.status_code,
                          resp_headers=dict(backend.headers),
                          resp_body=content, stream=True,
                          model=model, tokens=tok)
            await _save(rec)
            log.info("%s %s <- %d stream %.0fms tok=%s",
                     request.method, url, backend.status_code, ms, tok)

        return StreamingResponse(
            generate(),
            status_code=backend.status_code,
            headers=_resp_headers(backend.headers),
            media_type=backend.headers.get("content-type"),
            background=BackgroundTask(on_done),
        )

    # ── non-streaming ────────────────────────────────────────────────
    try:
        backend = await client.request(
            method=request.method, url=url, headers=headers,
            content=raw_body, params=params,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return _err502(rid, t0, request, url, req_json, exc)

    resp_body = backend.content
    resp_json = _parse_json(resp_body)
    ms = (time.monotonic() - t0) * 1000

    rec = _record(
        rid, ms, request, url, req_json,
        status=backend.status_code,
        resp_headers=dict(backend.headers),
        resp_body=resp_json if resp_json is not None else resp_body.decode(errors="replace"),
        stream=False,
        model=(resp_json or {}).get("model") if isinstance(resp_json, dict) else None,
        tokens=_tokens(resp_json) if isinstance(resp_json, dict) else None,
    )
    asyncio.create_task(_save(rec))
    log.info("%s %s <- %d %.0fms tok=%s",
             request.method, url, backend.status_code, ms, rec["meta"]["tokens"])

    return Response(
        content=resp_body,
        status_code=backend.status_code,
        headers=_resp_headers(backend.headers, strip_encoding=True),
        media_type=backend.headers.get("content-type"),
    )


# ── entrypoint ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("prism:app", host="0.0.0.0", port=LISTEN_PORT, log_level="info")
