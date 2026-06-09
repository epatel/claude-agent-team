import platform_client
from platform_client import extension_cli


class FakeServer:
    def __init__(self):
        self.ran_with = None

    def run(self, transport):
        self.ran_with = transport


def test_serve_builds_and_runs_sse_server():
    seen = {}
    server = FakeServer()

    def build_server(*, host, port):
        seen["host"], seen["port"] = host, port
        return server

    main = extension_cli("my-client", build_server, default_port=9001)
    assert main(["serve", "--host", "0.0.0.0"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9001}
    assert server.ran_with == "sse"


def test_no_command_prints_usage_without_serving(capsys):
    def build_server(**_):
        raise AssertionError("must not build a server without `serve`")

    main = extension_cli("my-client", build_server)
    assert main([]) == 0
    assert "my-client" in capsys.readouterr().out


def test_version():
    assert platform_client.__version__
