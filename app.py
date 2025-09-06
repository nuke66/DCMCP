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
import base64
from hmac import compare_digest
import json
from dotenv import load_dotenv

# Load .env early so env vars are available for middleware config
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


class SimpleAuthMiddleware:
    """
    Minimal ASGI auth middleware supporting Bearer and Basic auth.

    Configuration via environment variables (read at startup):
    - AUTH_BEARER_TOKEN or AUTH_TOKEN: shared bearer token value
    - BASIC_AUTH_USER: basic auth username
    - BASIC_AUTH_PASS: basic auth password
    - AUTH_EXEMPT_PATHS: comma-separated list of path prefixes to skip auth
    If no credentials are provided, middleware is effectively disabled.
    """

    def __init__(self, app) -> None:
        self.app = app
        # Read configuration once at startup
        self.bearer_token = os.environ.get("AUTH_BEARER_TOKEN") or os.environ.get("AUTH_TOKEN")
        self.basic_user = os.environ.get("BASIC_AUTH_USER")
        self.basic_pass = os.environ.get("BASIC_AUTH_PASS")
        tool_logger.info(f"SimpleAuthMiddleware config: basic_user={self.basic_user!r} basic_pass={self.basic_pass!r}")
        # To exempt the "add tool" endpoint from authentication, add its path prefix to AUTH_EXEMPT_PATHS.
        # For example, if the add tool endpoint is at "/add", set AUTH_EXEMPT_PATHS="/health,/metrics,/add"
        raw_exempt = os.environ.get("AUTH_EXEMPT_PATHS", "/health,/metrics")
        self.exempt_paths = tuple(p.strip() for p in raw_exempt.split(",") if p.strip())
        # Tools that should bypass auth during MCP tool calls (comma-separated list of tool names)
        raw_exempt_tools = os.environ.get("AUTH_EXEMPT_TOOLS", "")
        self.exempt_tools = {t.strip().lower() for t in raw_exempt_tools.split(",") if t.strip()}
        # Methods (MCP JSON-RPC method names) that may bypass auth for handshake/bootstrap
        raw_exempt_methods = os.environ.get(
            "AUTH_EXEMPT_METHODS",
            "initialize,tools/list,ping,resources/list,prompts/list"
        )
        self.exempt_methods = {m.strip().lower() for m in raw_exempt_methods.split(",") if m.strip()}
        # Method prefixes that may bypass auth (e.g., notifications/*)
        raw_exempt_method_prefixes = os.environ.get(
            "AUTH_EXEMPT_METHOD_PREFIXES",
            "notifications/"
        )
        self.exempt_method_prefixes = tuple(p.strip().lower() for p in raw_exempt_method_prefixes.split(",") if p.strip())
        # Some MCP clients open a GET /mcp stream; allow it optionally
        self.public_get_mcp = str(os.environ.get("AUTH_PUBLIC_GET_MCP", "false")).lower() in {"1", "true", "yes", "on"}
        self.enabled = bool(self.bearer_token or (self.basic_user and self.basic_pass))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.enabled:
            return await self.app(scope, receive, send)

        # Determine request path
        path = scope.get("raw_path") or scope.get("path", "/")
        if isinstance(path, (bytes, bytearray)):
            try:
                path = path.decode("utf-8", errors="ignore")
            except Exception:
                path = "/"

        # Determine method and prepare receive for potential replay if we need to inspect body
        method = (scope.get("method") or "").upper()
        receive_for_app = receive

        # Collect headers early so we can debug even if handshake is exempted
        try:
            early_headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        except Exception:
            early_headers = {}
        early_auth_header = early_headers.get("authorization", "")
        try:
            early_scheme = ""
            lower = early_auth_header.lower()
            if lower.startswith("bearer "):
                early_scheme = "bearer"
            elif lower.startswith("basic "):
                early_scheme = "basic"

            early_matched_bearer = False
            early_matched_basic = False
            if early_scheme == "bearer" and self.bearer_token:
                token = early_auth_header[7:].strip()
                early_matched_bearer = compare_digest(token, self.bearer_token)
            elif early_scheme == "basic" and self.basic_user and self.basic_pass:
                try:
                    b64 = early_auth_header[6:].strip()
                    decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                    username, password = (decoded.split(":", 1) + [""])[:2]
                    early_matched_basic = compare_digest(username, self.basic_user) and compare_digest(password, self.basic_pass)
                except Exception:
                    early_matched_basic = False
            auth_logger.info(
                f"early_auth_header present={(early_auth_header != '')} scheme={early_scheme} matched_bearer={early_matched_bearer} matched_basic={early_matched_basic}"
            )
        except Exception:
            pass

        # Skip exempt paths
        for prefix in self.exempt_paths:
            if path.startswith(prefix):
                return await self.app(scope, receive, send)

        # Allow GET /mcp (e.g., SSE/streaming) only if explicitly enabled
        if method == "GET" and path == "/mcp" and self.public_get_mcp:
            return await self.app(scope, receive, send)

        # If this is a POST to /mcp, attempt to inspect the JSON body to selectively bypass auth
        # for handshake methods and for specific tools listed in AUTH_EXEMPT_TOOLS.
        # We fully buffer and then replay the request body downstream.
        if method == "POST" and path.startswith("/mcp"):
            body_bytes = b""
            buffered_request_messages = []

            # Drain the incoming request body into memory
            try:
                while True:
                    message = await receive()
                    msg_type = message.get("type")
                    if msg_type == "http.request":
                        buffered_request_messages.append(message)
                        body_bytes += message.get("body", b"")
                        if not message.get("more_body", False):
                            break
                    else:
                        # Ignore non-body messages for replay, but stop if stream is clearly done
                        if not message.get("more_body", False):
                            break
            except Exception:
                # If anything goes wrong, ensure the body we have is still replayed downstream
                if body_bytes:
                    buffered_request_messages = [{"type": "http.request", "body": body_bytes, "more_body": False}]
                else:
                    buffered_request_messages = [{"type": "http.request", "body": b"", "more_body": False}]

            # Create a receive function that replays the buffered messages
            if buffered_request_messages:
                idx = 0

                async def replay_receive():
                    nonlocal idx
                    if idx < len(buffered_request_messages):
                        msg = buffered_request_messages[idx]
                        idx += 1
                        return msg
                    # Safety: return empty body if app calls again
                    return {"type": "http.request", "body": b"", "more_body": False}

                receive_for_app = replay_receive

                # Try to parse the body as JSON and detect a handshake method or a tools call to an exempt tool
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                    method_name = str(payload.get("method", "")).lower()
                    params = payload.get("params") or {}
                    tool_name = (
                        (params.get("name") or params.get("toolName") or params.get("tool_name") or "")
                    ).lower()

                    # Debug visibility into handshake/auth decisions
                    try:
                        auth_logger.info(
                            f"auth_probe path=/mcp method={method_name!r} tool={tool_name!r} exempt_method={(method_name in self.exempt_methods)} exempt_tool={(tool_name in self.exempt_tools)}"
                        )
                    except Exception:
                        pass

                    # Handshake/bootstrap methods may be allowed without auth
                    if (self.exempt_methods and method_name in self.exempt_methods) or (
                        self.exempt_method_prefixes and any(method_name.startswith(pref) for pref in self.exempt_method_prefixes)
                    ):
                        return await self.app(scope, receive_for_app, send)

                    if self.exempt_tools and method_name in ("tools/call", "tool/call", "call_tool") and tool_name in self.exempt_tools:
                        # Bypass auth only for the configured tools
                        return await self.app(scope, receive_for_app, send)
                except Exception:
                    # If parsing fails, continue with normal auth checks
                    pass

        # Collect headers
        try:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        except Exception:
            headers = {}

        auth_header = headers.get("authorization", "")

        # Debug: note presence and match of Authorization header without exposing secrets
        try:
            scheme = ""
            lower = auth_header.lower()
            if lower.startswith("bearer "):
                scheme = "bearer"
            elif lower.startswith("basic "):
                scheme = "basic"

            matched_bearer = False
            matched_basic = False
            if scheme == "bearer" and self.bearer_token:
                token = auth_header[7:].strip()
                matched_bearer = compare_digest(token, self.bearer_token)
            elif scheme == "basic" and self.basic_user and self.basic_pass:
                try:
                    b64 = auth_header[6:].strip()
                    decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                    username, password = (decoded.split(":", 1) + [""])[:2]
                    matched_basic = compare_digest(username, self.basic_user) and compare_digest(password, self.basic_pass)
                except Exception:
                    matched_basic = False
            auth_logger.info(
                f"auth_header present={(auth_header != '')} scheme={scheme} matched_bearer={matched_bearer} matched_basic={matched_basic}"
            )
        except Exception:
            pass

        # Bearer auth
        if self.bearer_token and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if compare_digest(token, self.bearer_token):
                return await self.app(scope, receive_for_app, send)

        # Basic auth
        if self.basic_user and self.basic_pass and auth_header.lower().startswith("basic "):
            b64 = auth_header[6:].strip()
            try:
                decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                username, password = (decoded.split(":", 1) + [""])[:2]
                if compare_digest(username, self.basic_user) and compare_digest(password, self.basic_pass):
                    return await self.app(scope, receive_for_app, send)
            except Exception:
                pass

        # Unauthorized
        headers_list = [
            [b"content-type", b"application/json"],
            [b"www-authenticate", b'Bearer realm="mcp"'],
        ]

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": headers_list,
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail": "Unauthorized"}',
            "more_body": False,
        })


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

# Auth/debug logger
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

@mcp.tool
@log_tool
def get_weather_for_today() -> str:
    """
    Returns weather forecast for today.
    """
   
    return "Today's weather is sunny"



app = mcp.http_app(path="/mcp")
# Ensure logging remains outermost by adding it after auth
app.add_middleware(SimpleAuthMiddleware)
app.add_middleware(StreamingSafeRequestLoggingMiddleware)