from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
import pandas as pd

mcp = FastAPI()




@mcp.get("/add", operation_id="add_two_numbers")
async def add(a: int, b: int):
    return {"sum": int(pd.DataFrame({"a":[a], "b":[b]}).eval("a+b").iloc[0])}

@mcp.get("/get_event_descriptions", operation_id="get_event_descriptions")
async def get_event_descriptions():
    """
    Return a list of all event names and their descriptions from the data catalog.  They key is the event name, the value is the description.
    """
    event_descriptions = {
        "component_interaction": "When a user has any of the following interactions with a component on the website:  click, expand, collapse, check, uncheck, open, close",
        "wayfinder_start": "This event should be triggered when a user clicks on one of the first wayfinder questions, located on the homepage.",
        "wayfinder_next": "This event should be triggered when a user clicks ‘next’ as they progress through the wayfinder tool. It should also be triggered if a user navigates back and then updates their selection.",
        "wayfinder_back": "This event should be triggered when a user clicks ‘back’ as they progress through the wayfinder tool.",
        "wayfinder_complete": "This event should be triggered when a user completes the wayfinder tool ie. by pressing the final button on their selected flow (‘What’s my next step?').",
        "outlet_interaction": "This interaction fires across multiple areas of the site - when a user interacts with an outlet card. \n\nInteractions may include: click to call, add to shortlist, see details click, add to compare, remove from compare, add note.",
        "outlet_view": "This event will fire when a user views an Outlet detail page (of any care type)."
        }
    return event_descriptions

mcp_server = FastApiMCP(
    mcp,
    name="Data Catalog MCP",
    description="MCP server exposing information about the MAC data catalog"
)
mcp_server.mount_http()