import os
import tempfile

from src.state import read_known_urls, read_last_sha, write_known_urls, write_last_sha


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


def test_read_known_urls_returns_empty_set_when_file_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = read_known_urls(tmpdir)
        assert result == set()


def test_write_and_read_known_urls_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        urls = {"https://example.com/a", "https://example.com/b"}
        write_known_urls(tmpdir, urls)
        result = read_known_urls(tmpdir)
        assert result == urls


def test_read_known_urls_returns_set_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        write_known_urls(tmpdir, {"https://a.com", "https://b.com"})
        result = read_known_urls(tmpdir)
        assert isinstance(result, set)
        assert len(result) == 2


def test_write_known_urls_empty_set():
    with tempfile.TemporaryDirectory() as tmpdir:
        write_known_urls(tmpdir, set())
        result = read_known_urls(tmpdir)
        assert result == set()
