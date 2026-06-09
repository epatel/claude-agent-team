import pytest
from platform_client import manifest as m


def _tree(root, files: dict[str, str]):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_scan_hashes_files_and_skips_ignores(tmp_path):
    _tree(tmp_path, {"a.py": "x", "pkg/b.py": "y", ".git/conf": "no", ".venv/lib": "no"})
    scanned = m.scan(tmp_path)
    assert set(scanned) == {"a.py", "pkg/b.py"}
    assert all(len(h) == 64 for h in scanned.values())


def test_scan_skips_symlinks(tmp_path):
    _tree(tmp_path, {"a.py": "x"})
    (tmp_path / "link").symlink_to(tmp_path / "a.py")
    assert set(m.scan(tmp_path)) == {"a.py"}


def test_manifest_hash_is_stable_and_content_sensitive(tmp_path):
    _tree(tmp_path, {"a.py": "x", "b.py": "y"})
    h1 = m.manifest_hash(m.scan(tmp_path))
    assert h1 == m.manifest_hash(m.scan(tmp_path))
    (tmp_path / "a.py").write_text("changed")
    assert m.manifest_hash(m.scan(tmp_path)) != h1


def test_delta_reports_need_and_delete(tmp_path):
    source = {"same.py": "h1", "stale.py": "h2", "new.py": "h3"}
    mirror = {"same.py": "h1", "stale.py": "old", "gone.py": "h4"}
    need, delete = m.delta(source, mirror)
    assert need == ["new.py", "stale.py"]
    assert delete == ["gone.py"]


def test_changed_reports_added_modified_deleted():
    before = {"keep.py": "h1", "mod.py": "h2", "del.py": "h3"}
    after = {"keep.py": "h1", "mod.py": "new", "add.py": "h4"}
    assert m.changed(before, after) == {
        "added": ["add.py"],
        "modified": ["mod.py"],
        "deleted": ["del.py"],
    }


def test_sync_round_trip_makes_mirror_match_source(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src, {"a.py": "x", "pkg/b.py": "y"})
    _tree(dst, {"pkg/b.py": "old", "junk.py": "z"})

    source = m.scan(src)
    need, delete = m.delta(source, m.scan(dst))
    for rel in need:
        m.write_file(dst, rel, m.read_file(src, rel))
    m.delete_files(dst, delete)

    assert m.scan(dst) == source


def test_paths_cannot_escape_root(tmp_path):
    with pytest.raises(m.PathOutsideRoot):
        m.read_file(tmp_path, "../outside")
    with pytest.raises(m.PathOutsideRoot):
        m.write_file(tmp_path, "/etc/owned", b"")
    with pytest.raises(m.PathOutsideRoot):
        m.delete_files(tmp_path, ["a/../../b"])


def test_oversized_files_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_FILE_BYTES", 4)
    _tree(tmp_path, {"small.txt": "ok", "big.txt": "too large"})
    assert set(m.scan(tmp_path)) == {"small.txt"}
