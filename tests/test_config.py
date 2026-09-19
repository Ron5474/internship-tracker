import pytest

from config import FEEDS, FeedSpec, load_settings


def test_feeds_registry_has_both_feeds():
    assert FEEDS["internships"] == FeedSpec("internships", "SimplifyJobs/Summer2026-Internships", "dev")
    assert FEEDS["new-grad"] == FeedSpec("new-grad", "SimplifyJobs/New-Grad-Positions", "dev")


def test_load_settings_defaults():
    s = load_settings({})
    assert s.data_dir == "/data"
    assert s.poll_interval == 300
    assert s.github_token is None


def test_load_settings_reads_env():
    s = load_settings({"DATA_DIR": "/tmp/x", "POLL_INTERVAL_SECONDS": "60", "GITHUB_TOKEN": "ghp_1"})
    assert s.data_dir == "/tmp/x"
    assert s.poll_interval == 60
    assert s.github_token == "ghp_1"


def test_load_settings_treats_blank_token_as_none():
    assert load_settings({"GITHUB_TOKEN": ""}).github_token is None


def test_load_settings_rejects_non_integer_interval():
    with pytest.raises(ValueError):
        load_settings({"POLL_INTERVAL_SECONDS": "soon"})
