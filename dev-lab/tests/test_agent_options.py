from dev_lab.agent import build_agent_options


def test_options_without_extensions():
    opts = build_agent_options(cwd="/tmp", model="m")
    assert opts.mcp_servers == {}
    assert "Bash" in opts.allowed_tools
    assert not any(t.startswith("mcp__") for t in opts.allowed_tools)


def test_options_with_extensions():
    opts = build_agent_options(
        cwd="/tmp", model="m", extensions={"macos": "http://h:8970/sse"}
    )
    assert opts.mcp_servers["macos"] == {"type": "sse", "url": "http://h:8970/sse"}
    assert "mcp__macos" in opts.allowed_tools
