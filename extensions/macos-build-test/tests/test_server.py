import asyncio

from macos_build_test.server import build_server


def test_capability_tools_registered():
    mcp = build_server()
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {"run_tests", "build"}
