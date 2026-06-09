import subprocess
from pathlib import Path

from platform_client import run_in_checkout


def _make_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y.z"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "run.sh").write_text("echo built ok\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_run_in_checkout_success(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    res = run_in_checkout(str(repo), "HEAD", "sh run.sh")
    assert res.ok
    assert res.returncode == 0
    assert "built ok" in res.stdout


def test_run_in_checkout_failing_command(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    res = run_in_checkout(str(repo), "HEAD", "exit 3")
    assert not res.ok
    assert res.returncode == 3


def test_run_in_checkout_bad_ref(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    res = run_in_checkout(str(repo), "does-not-exist", "true")
    assert not res.ok
