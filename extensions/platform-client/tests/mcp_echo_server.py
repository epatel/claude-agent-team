"""A minimal stdio MCP server for bridge tests: one echo tool."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()
