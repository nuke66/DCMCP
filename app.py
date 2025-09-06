from fastmcp import FastMCP  # user the FastMCP v2.0 implementation which is faster https://gofastmcp.com/getting-started/installation
import pandas as pd
import logging
import time
import uuid
from typing import Callable, Awaitable, Optional
import inspect
from functools import wraps
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import urllib.parse

# Load environment variables from a .env file if present
load_dotenv()

mcp = FastMCP(
    name="Data Catalog MCP",
    instructions="MCP server exposing information about the data catalog"
)


class StreamingSafeRequestLoggingMiddleware:
    """
    ASGI middleware that logs requests without interfering with streaming/SSE.

    Avoids Starlette's BaseHTTPMiddleware which can buffer responses and break streams.
    """

    def __init__(self, app, logger: Optional[logging.Logger] = None) -> None:
        self.app = app
        self.logger = logger or logging.getLogger("mcp.request")
        _setup_logger_with_stream_and_file(self.logger, "requests.log")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        start_time = time.perf_counter()

        # Extract inbound request info
        method = scope.get("method", "-")
        path = scope.get("raw_path") or scope.get("path", "-")
        if isinstance(path, (bytes, bytearray)):
            try:
                path = path.decode("utf-8", errors="ignore")
            except Exception:
                path = "-"
        client_host = "-"
        if scope.get("client") and isinstance(scope["client"], (list, tuple)) and scope["client"]:
            client_host = scope["client"][0] or "-"

        # Correlate request id
        request_id = None
        try:
            headers = dict((k.lower(), v) for k, v in ((h[0].decode(), h[1].decode()) for h in scope.get("headers", [])))
            request_id = headers.get("x-request-id")
        except Exception:
            request_id = None
        if not request_id:
            request_id = str(uuid.uuid4())

        status_code_holder = {"status": 200}
        logged = {"done": False}

        async def send_wrapper(message):
            # Inject correlation header on response start
            if message.get("type") == "http.response.start":
                status_code_holder["status"] = int(message.get("status", 200))
                headers = list(message.get("headers", []))
                try:
                    headers.append([b"x-request-id", request_id.encode("utf-8")])
                except Exception:
                    pass
                message["headers"] = headers

            # When the body is finished (no more_body), log the request
            if message.get("type") == "http.response.body" and not message.get("more_body", False) and not logged["done"]:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                self.logger.info(
                    f'{client_host} - "{method} {path}" {status_code_holder["status"]} {duration_ms}ms req_id={request_id}'
                )
                logged["done"] = True

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Ensure we log even on error paths
            if not logged["done"]:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                self.logger.info(
                    f'{client_host} - "{method} {path}" 500 {duration_ms}ms req_id={request_id}'
                )
            raise


def _setup_logger_with_stream_and_file(logger: logging.Logger, filename: str) -> None:
    """Attach a stream handler and a rotating file handler to the given logger.

    Logs are written to stdout and to a file under LOG_DIR (defaults to shared_data/logs).
    """
    # Ensure logs are emitted even if root/uvicorn configs don't include this logger
    logger.setLevel(logging.INFO)
    if logger.handlers:
        # Assume logger is already configured
        logger.propagate = False
        return

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Stream handler (console)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # File handler (rotating)
    base_dir = Path(os.environ.get("LOG_DIR", str(Path("shared_data") / "logs")))
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(base_dir / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        # If file handler fails, we still have the stream handler; log a warning once
        fallback_logger = logging.getLogger("mcp.init")
        fallback_logger.setLevel(logging.INFO)
        if not fallback_logger.handlers:
            fallback_stream = logging.StreamHandler()
            fallback_stream.setFormatter(formatter)
            fallback_logger.addHandler(fallback_stream)
        fallback_logger.warning(f"Failed to initialize file logging for {logger.name}: {e}")

    # Prevent double-logging if parent/root also handles records
    logger.propagate = False


# Log tool requests and their parameters
tool_logger = logging.getLogger("mcp.tool")
_setup_logger_with_stream_and_file(tool_logger, "tools.log")
auth_logger = logging.getLogger("mcp.auth")
_setup_logger_with_stream_and_file(auth_logger, "auth.log")


def log_tool(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
        except Exception:
            params = {"args": args, "kwargs": kwargs}
        tool_logger.info(f'tool_request name={func.__name__} params={params}')
        return func(*args, **kwargs)
    return wrapper


class TokenAuthMiddleware:
    """
    Minimal ASGI auth middleware checking a shared token.

    - Reads expected token from AUTH_TOKEN env var
    - Accepts either Authorization: Bearer <token> header or ?token=<token> query param
    - If AUTH_TOKEN is empty or not set, auth is disabled (requests are allowed)
    """

    def __init__(self, app) -> None:
        self.app = app
        self.expected_token = os.environ.get("AUTH_TOKEN", "").strip()
        # Log once if auth disabled
        if not self.expected_token:
            auth_logger.info("AUTH_TOKEN not set; authentication is DISABLED")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        # Skip check if no token configured
        if not self.expected_token:
            return await self.app(scope, receive, send)

        # Extract token from Authorization header or query string
        token = None
        try:
            raw_headers = scope.get("headers", [])
            headers = {k.decode().lower(): v.decode() for k, v in raw_headers}
            auth_header = headers.get("authorization", "")
            if auth_header:
                parts = auth_header.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1]
        except Exception:
            token = None

        if not token:
            try:
                raw_qs = scope.get("query_string", b"")
                if isinstance(raw_qs, (bytes, bytearray)):
                    raw_qs = raw_qs.decode("utf-8", errors="ignore")
                qs = urllib.parse.parse_qs(raw_qs, keep_blank_values=True)
                token_candidates = qs.get("token") or []
                if token_candidates:
                    token = token_candidates[0]
            except Exception:
                token = None

        if token == self.expected_token:
            return await self.app(scope, receive, send)

        # Unauthorized
        message = b'{"detail":"Unauthorized"}'
        headers = [
            [b"content-type", b"application/json"],
            [b"www-authenticate", b"Bearer"],
        ]
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": message,
            "more_body": False,
        })
        return

@mcp.tool
@log_tool
def add(a: int, b: int) -> dict:
    """Add two numbers and return the sum as {\"sum\": int}."""
    return {"sum": int(pd.DataFrame({"a": [a], "b": [b]}).eval("a+b").iloc[0])}


@mcp.tool
@log_tool
def get_event_descriptions() -> dict:
    """
    Return a mapping of all event names and their descriptions from the data catalog.
    The key is the event name, the value is the description.
    """
    event_descriptions = {
        "open_seasame": "Send when a user opens the xyz component",
        "show_more": "Event fired then user clicks on the show me more button",
        "see_more": "This event tracks when a users clicks a pagination link to view another set of results"
    }
    return event_descriptions

app = mcp.http_app(path="/mcp")
app.add_middleware(StreamingSafeRequestLoggingMiddleware)
app.add_middleware(TokenAuthMiddleware)