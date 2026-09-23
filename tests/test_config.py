import pytest

from config import FEEDS, FeedSpec, load_settings

LLM = {"LLM_BASE_URL": "http://litellm:4000/v1", "LLM_SCORE_MODEL": "deepseek-v4-flash", "LLM_TAILOR_MODEL": "pro"}


def test_feeds_registry_has_both_feeds():
    assert FEEDS["internships"] == FeedSpec("internships", "SimplifyJobs/Summer2026-Internships", "dev")
    assert FEEDS["new-grad"] == FeedSpec("new-grad", "SimplifyJobs/New-Grad-Positions", "dev")


def test_load_settings_defaults():
    s = load_settings(LLM)
    assert s.data_dir == "/data"
    assert s.poll_interval == 300
    assert s.github_token is None
    assert s.llm_base_url == "http://litellm:4000/v1"
    assert s.llm_api_key is None
    assert s.llm_score_model == "deepseek-v4-flash"
    assert s.llm_timeout == 120


def test_load_settings_reads_env():
    s = load_settings({**LLM, "DATA_DIR": "/tmp/x", "POLL_INTERVAL_SECONDS": "60", "GITHUB_TOKEN": "ghp_1",
                       "LLM_API_KEY": "sk-1", "LLM_TIMEOUT_SECONDS": "30"})
    assert s.data_dir == "/tmp/x" and s.poll_interval == 60 and s.github_token == "ghp_1"
    assert s.llm_api_key == "sk-1" and s.llm_timeout == 30


def test_load_settings_strips_trailing_slash_from_llm_base_url():
    assert load_settings({**LLM, "LLM_BASE_URL": "http://h:4000/v1/"}).llm_base_url == "http://h:4000/v1"


def test_load_settings_requires_llm_base_url():
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        load_settings({"LLM_SCORE_MODEL": "m"})


def test_load_settings_requires_llm_score_model():
    with pytest.raises(ValueError, match="LLM_SCORE_MODEL"):
        load_settings({"LLM_BASE_URL": "http://h"})


def test_load_settings_treats_blank_token_as_none():
    assert load_settings({**LLM, "GITHUB_TOKEN": ""}).github_token is None


def test_load_settings_rejects_non_integer_interval():
    with pytest.raises(ValueError):
        load_settings({**LLM, "POLL_INTERVAL_SECONDS": "soon"})


def test_tailor_model_required():
    env = {"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "flash"}
    with pytest.raises(ValueError, match="LLM_TAILOR_MODEL"):
        load_settings(env)


def test_tailor_model_and_bullet_cap_parsed():
    s = load_settings({
        "LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "flash",
        "LLM_TAILOR_MODEL": "pro", "MAX_BULLETS_PER_ENTRY": "3",
    })
    assert s.llm_tailor_model == "pro"
    assert s.max_bullets_per_entry == 3


@pytest.mark.parametrize("bad", ["0", "-2"])
def test_bullet_cap_must_be_positive(bad):
    with pytest.raises(ValueError, match="MAX_BULLETS_PER_ENTRY"):
        load_settings({"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "f",
                       "LLM_TAILOR_MODEL": "p", "MAX_BULLETS_PER_ENTRY": bad})


def test_bullet_cap_defaults_to_four():
    s = load_settings({"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "f", "LLM_TAILOR_MODEL": "p"})
    assert s.max_bullets_per_entry == 4
