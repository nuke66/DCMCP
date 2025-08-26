# DCMCP
Data catalog MCP server using FastMCP, runnable locally or via Docker.


## Run locally

Install deps (recommended with `uv`):

```bash
uv pip install -r requirements.txt
```

Start the HTTP server:

```bash
uvicorn app:app --host 0.0.0.0 --port 8005 --log-level debug
```

The MCP HTTP endpoint will be served at `http://localhost:8005/`.


## Docker

```bash
docker compose up --build
```


## Claude Desktop config (HTTP transport)

```json
{
  "mcpServers": {
    "dcmcp": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:8005/",
        "--transport",
        "http-first",
        "--allow-http"
      ]
    }
  }
}
```


## References

- Official Python SDK for MCP server: `https://github.com/modelcontextprotocol/python-sdk`
