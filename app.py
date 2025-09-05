from fastmcp import FastMCP  # user the FastMCP v2.0 implementation which is faster https://gofastmcp.com/getting-started/installation
import pandas as pd
import logging
import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware

mcp = FastMCP(
    name="Data Catalog MCP",
    instructions="MCP server exposing information about the data catalog"
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger: logging.Logger | None = None) -> None:
        super().__init__(app)
        # Use our own logger to avoid uvicorn's AccessFormatter tuple expectations
        self.logger = logger or logging.getLogger("mcp.request")
        # Ensure logs are emitted even if root/uvicorn configs don't include this logger
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            _handler = logging.StreamHandler()
            _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(_handler)
        # Prevent double-logging if parent/root also handles records
        self.logger.propagate = False

    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        start_time = time.perf_counter()

        response = await call_next(request)

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        client = request.client.host if getattr(request, "client", None) else "-"

        # Log in a format similar to common access logs with extras
        self.logger.info(
            f'{client} - "{request.method} {request.url.path}" {response.status_code} {duration_ms}ms req_id={request_id}'
        )

        # Echo request id back to caller for correlation
        response.headers["x-request-id"] = request_id
        return response


@mcp.tool
def add(a: int, b: int) -> dict:
    """Add two numbers and return the sum as {\"sum\": int}."""
    return {"sum": int(pd.DataFrame({"a": [a], "b": [b]}).eval("a+b").iloc[0])}


@mcp.tool
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
app.add_middleware(RequestLoggingMiddleware)