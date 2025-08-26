# DCMCP
Data catalog MCP server running on Docker.


This project uses UV.


official python sdk for mcp server
https://github.com/modelcontextprotocol/python-sdk




claude_desktop_config.json
```
{
  "mcpServers": {
    "fastapi-mcp": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://localhost:8005/mcp/",
        "--transport", "http-first",
        "--allow-http"
      ]
    }
  }
}

```




