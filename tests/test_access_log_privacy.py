"""Access logs retain routing/status information without URL query contents."""
import asyncio
import io
import logging
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["h11", "httptools"])
async def test_real_http_logs_hide_queries_without_altering_input(protocol):
    # Exercise the server logger, not an ASGI-only transport that has no logs.
    output = io.StringIO()
    logger = logging.getLogger("uvicorn.access")
    previous = (logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
    handler = logging.StreamHandler(output)
    handler.setFormatter(uvicorn.logging.AccessFormatter('{request_line} {status_code}', style='{', use_colors=False))
    logger.handlers, logger.filters = [handler], []
    logger.setLevel(logging.INFO)
    logger.propagate = False

    @asynccontextmanager
    async def lifespan(app):
        main.install_access_log_filter()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/ai/generate-image/suggest-tags")
    async def echo(request: Request):
        return {"prompt": request.query_params.get("prompt")}

    @app.get("/denied")
    async def denied():
        return JSONResponse({"error": "fixture"}, status_code=401)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                            http=protocol, log_config=None, access_log=True))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        marker = "PRIVATE_LOG_FIXTURE&星?x=secret"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            response = await client.get("/ai/generate-image/suggest-tags", params={"prompt": marker, "model": "fixture"})
            assert response.json() == {"prompt": marker}
            assert (await client.get("/denied", params={"token": marker})).status_code == 401
            assert (await client.get("/missing", params={"anything": marker})).status_code == 404
        logs = output.getvalue()
        assert "PRIVATE_LOG_FIXTURE" not in logs and "?" not in logs
        assert "GET /ai/generate-image/suggest-tags HTTP/1.1 200" in logs
        assert "GET /denied HTTP/1.1 401" in logs
        assert "GET /missing HTTP/1.1 404" in logs
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
        logger.handlers, logger.filters, logger.level, logger.propagate = previous


@pytest.mark.parametrize("path", ["/healthz", "/tags?prompt=private", "/tags?prompt=%3Fprivate%26a%3Db", "/tags?prompt=a?b"])
def test_filter_preserves_path_and_status(path):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1,
                               '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", 200), None)
    assert main.QuerylessAccessFilter().filter(record)
    assert record.args == ("127.0.0.1:1", "GET", path.partition("?")[0], "1.1", 200)


@pytest.mark.parametrize("message,args", [("private URL already formatted", ()), ("%s", ("private",)),
                                          ('%s - "%s %s HTTP/%s" %d', ("peer", "GET", None, "1.1", 200))])
def test_unrecognized_access_record_is_not_emitted(message, args):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1, message, args, None)
    assert main.QuerylessAccessFilter().filter(record) is False


def test_install_is_idempotent_and_does_not_touch_error_logger():
    logger = logging.getLogger("uvicorn.access")
    previous = logger.filters[:]
    error_filters = logging.getLogger("uvicorn.error").filters[:]
    try:
        logger.filters = []
        main.install_access_log_filter()
        main.install_access_log_filter()
        assert len(logger.filters) == 1
        assert logging.getLogger("uvicorn.error").filters == error_filters
    finally:
        logger.filters = previous
