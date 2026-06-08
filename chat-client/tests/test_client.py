import chat_client
from chat_client.client import format_event


def test_version():
    assert chat_client.__version__


def test_format_known_events():
    assert "queued" in format_event({"type": "ack", "job_id": "j1", "instruction": "x"})
    assert "running" in format_event({"type": "job_running", "job_id": "j1", "instruction": "x"})
    assert format_event({"type": "agent_message", "text": "hello"}) == "hello"
    assert "failed" in format_event({"type": "job_failed", "job_id": "j1", "error": "boom"})


def test_format_job_done_truncates_sha():
    line = format_event(
        {"type": "job_done", "job_id": "j1", "branch": "lab/x", "commit_sha": "a" * 40}
    )
    assert "aaaaaaaaaaaa" in line
    assert "a" * 40 not in line


def test_format_unknown_falls_back_to_json():
    assert "mystery" in format_event({"type": "mystery", "k": 1})
