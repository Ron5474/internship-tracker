import json
from unittest.mock import Mock, patch

import requests

from llm import LLMClient, LLMResult, ScoreResponse, classify_status
from prompts import SCORE_SYSTEM, score_user_message

GOOD = {"score": 82, "reasoning": "Strong Python and FastAPI match.", "missing_confirmed": ["Kubernetes"],
        "missing_unknown": ["work authorization"]}


def _resp(status, content=None, headers=None, usage=None, raw=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    body = raw if raw is not None else {
        "choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}],
        "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 80},
        "model": "deepseek-v4-flash",
    }
    r.json.return_value = body
    r.text = json.dumps(body) if isinstance(body, dict) else str(body)
    return r


def _client():
    return LLMClient("http://llm:4000/v1", "sk-test", "deepseek-v4-flash", timeout=7)


# --- prompts -----------------------------------------------------------------

def test_system_prompt_carries_rubric_and_schema():
    flat = " ".join(SCORE_SYSTEM.split())
    assert "90–100" in flat and "below 50" in flat
    assert '"missing_unknown"' in flat
    assert "does not lower the score" in flat
    # Location mismatches are the candidate's call: unknown, never confirmed-missing.
    assert "relocation" in flat and flat.index("Location:") < flat.index("relocation")
    assert "no hard requirements" in flat
    assert "400 characters" in flat
    assert '"posting_usable": <true|false>' in flat


def test_user_message_contains_both_inputs_in_order():
    m = score_user_message("JOB TEXT", "CV TEXT")
    assert m.index("JOB TEXT") < m.index("CV TEXT")
    assert m.endswith("Return the JSON object now.")


# --- classify ----------------------------------------------------------------

def test_classify_status():
    assert classify_status(200) == "ok"
    assert classify_status(429) == "transient" and classify_status(503) == "transient"
    assert classify_status(408) == "transient"
    assert classify_status(401) == "unavailable" and classify_status(403) == "unavailable" and classify_status(404) == "unavailable"
    assert classify_status(400) == "invalid" and classify_status(422) == "invalid"


# --- score -------------------------------------------------------------------

def test_score_posts_json_mode_with_auth_and_returns_parsed():
    with patch("llm.requests.post", return_value=_resp(200, GOOD)) as post:
        r = _client().score("JD", "CV")
    assert r.ok and isinstance(r.data, ScoreResponse) and r.data.score == 82
    assert r.model == "deepseek-v4-flash" and r.usage["prompt_tokens"] == 1200 and r.ms >= 0
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "http://llm:4000/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["timeout"] == 7
    body = kwargs["json"]
    assert body["model"] == "deepseek-v4-flash" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": SCORE_SYSTEM}
    assert body["messages"][1]["role"] == "user" and "JD" in body["messages"][1]["content"]


def test_score_without_api_key_sends_no_auth_header():
    with patch("llm.requests.post", return_value=_resp(200, GOOD)) as post:
        LLMClient("http://llm/v1", None, "m", timeout=5).score("JD", "CV")
    assert "Authorization" not in post.call_args.kwargs["headers"]


def test_score_tolerates_code_fenced_json():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    with patch("llm.requests.post", return_value=_resp(200, fenced)):
        assert _client().score("JD", "CV").data.score == 82


def test_score_rejects_non_integer_or_out_of_range_scores():
    # Each of these must be re-asked and, when repeated, classed invalid — never coerced.
    for bad in [140, -1, "82", 82.5, True, None]:
        with patch("llm.requests.post", side_effect=[_resp(200, {**GOOD, "score": bad})] * 2) as post:
            r = _client().score("JD", "CV")
        assert r.kind == "invalid", bad
        assert post.call_count == 2, bad


def test_score_requires_reasoning_and_gap_fields():
    for missing in ["reasoning", "missing_confirmed", "missing_unknown"]:
        body = {k: v for k, v in GOOD.items() if k != missing}
        with patch("llm.requests.post", side_effect=[_resp(200, body)] * 2):
            assert _client().score("JD", "CV").kind == "invalid", missing


def test_score_parses_posting_usable_false():
    body = {"score": 0, "reasoning": "Not a posting.", "missing_confirmed": [], "missing_unknown": [], "posting_usable": False}
    with patch("llm.requests.post", return_value=_resp(200, body)):
        r = _client().score("JD", "CV")
    assert r.ok and r.data.posting_usable is False


def test_score_posting_usable_defaults_true():
    with patch("llm.requests.post", return_value=_resp(200, GOOD)):
        assert _client().score("JD", "CV").data.posting_usable is True


def test_score_posting_usable_must_be_a_json_boolean():
    for bad in ["false", 0, None]:
        with patch("llm.requests.post", side_effect=[_resp(200, {**GOOD, "posting_usable": bad})] * 2):
            assert _client().score("JD", "CV").kind == "invalid", bad


def test_score_accepts_empty_lists():
    with patch("llm.requests.post", return_value=_resp(200, {**GOOD, "missing_confirmed": [], "missing_unknown": []})):
        assert _client().score("JD", "CV").ok


def test_score_reasks_once_on_invalid_json_then_succeeds():
    with patch("llm.requests.post", side_effect=[_resp(200, "not json at all"), _resp(200, GOOD)]) as post:
        r = _client().score("JD", "CV")
    assert r.ok
    assert post.call_count == 2
    msgs = post.call_args.kwargs["json"]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": "not json at all"}
    assert msgs[-1]["role"] == "user" and "JSON" in msgs[-1]["content"]


def test_reask_message_includes_validation_error():
    # The re-ask names the failing field so the model can fix that, not guess.
    with patch("llm.requests.post", side_effect=[_resp(200, {**GOOD, "score": 82.0}), _resp(200, GOOD)]) as post:
        r = _client().score("JD", "CV")
    assert r.ok and post.call_count == 2
    reask = post.call_args.kwargs["json"]["messages"][-1]
    assert reask["role"] == "user" and "score" in reask["content"]


def test_score_usage_sums_both_calls_on_reask():
    first = _resp(200, "not json", usage={"prompt_tokens": 100, "completion_tokens": 10})
    second = _resp(200, GOOD, usage={"prompt_tokens": 120, "completion_tokens": 30})
    with patch("llm.requests.post", side_effect=[first, second]):
        r = _client().score("JD", "CV")
    assert r.ok
    assert r.usage == {"prompt_tokens": 220, "completion_tokens": 40}


def test_client_exposes_model_name():
    assert _client().model == "deepseek-v4-flash"


def test_score_invalid_after_reask_is_invalid():
    with patch("llm.requests.post", side_effect=[_resp(200, "nope"), _resp(200, {"score": "high"})]) as post:
        r = _client().score("JD", "CV")
    assert r.kind == "invalid" and r.data is None and post.call_count == 2
    assert "score" in r.error


def test_score_missing_choices_is_invalid():
    with patch("llm.requests.post", return_value=_resp(200, raw={"error": "weird"})):
        assert _client().score("JD", "CV").kind == "invalid"


def test_score_429_is_transient_with_retry_after():
    with patch("llm.requests.post", return_value=_resp(429, raw={}, headers={"Retry-After": "12"})):
        r = _client().score("JD", "CV")
    assert r.kind == "transient" and r.retry_after == 12.0


def test_score_5xx_is_transient():
    with patch("llm.requests.post", return_value=_resp(502, raw={})):
        assert _client().score("JD", "CV").kind == "transient"


def test_score_timeout_is_transient():
    with patch("llm.requests.post", side_effect=requests.Timeout("slow")):
        r = _client().score("JD", "CV")
    assert r.kind == "transient" and "slow" in r.error


def test_score_connection_error_is_unavailable():
    with patch("llm.requests.post", side_effect=requests.ConnectionError("refused")):
        r = _client().score("JD", "CV")
    assert r.kind == "unavailable" and "refused" in r.error


def test_score_401_is_unavailable():
    with patch("llm.requests.post", return_value=_resp(401, raw={"error": {"message": "bad key"}})):
        r = _client().score("JD", "CV")
    assert r.kind == "unavailable" and "bad key" in r.error


def test_score_400_is_invalid():
    with patch("llm.requests.post", return_value=_resp(400, raw={"error": {"message": "context length"}})):
        assert _client().score("JD", "CV").kind == "invalid"


def test_llm_result_ok_property():
    assert LLMResult("ok", ScoreResponse(**GOOD), None, None, "m", None, 1).ok
    assert not LLMResult("transient", None, "x", None, None, None, 1).ok
