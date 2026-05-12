import os
import tempfile

from src.state import read_last_sha, write_last_sha


def test_read_last_sha_returns_none_when_file_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = read_last_sha(tmpdir)
        assert result is None


def test_write_and_read_last_sha_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        write_last_sha(tmpdir, "abc123def456")
        result = read_last_sha(tmpdir)
        assert result == "abc123def456"


def test_read_last_sha_strips_trailing_newline():
    with tempfile.TemporaryDirectory() as tmpdir:
        sha_path = os.path.join(tmpdir, "last_sha.txt")
        with open(sha_path, "w") as f:
            f.write("abc123\n")
        result = read_last_sha(tmpdir)
        assert result == "abc123"


def test_write_last_sha_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        nested_dir = os.path.join(tmpdir, "a", "b", "c")
        write_last_sha(nested_dir, "xyz789")
        result = read_last_sha(nested_dir)
        assert result == "xyz789"


def test_read_last_sha_returns_none_for_empty_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        sha_path = os.path.join(tmpdir, "last_sha.txt")
        with open(sha_path, "w") as f:
            f.write("")
        result = read_last_sha(tmpdir)
        assert result is None
