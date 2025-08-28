from fastmcp import FastMCP  # user the FastMCP v2.0 implementation which is faster https://gofastmcp.com/getting-started/installation
import pandas as pd

mcp = FastMCP(
    name="Data Catalog MCP",
    instructions="MCP server exposing information about the data catalog"
)


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