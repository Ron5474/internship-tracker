"""DecisionClient: scoring through LiteLLM's /decisions route (TypeSafe Jev)."""
from unittest.mock import Mock

import requests

from llm import DecisionClient

JEV = "jev"


def _answers(tech=0.95, fit=3.0, posting=0.97, fit_confidence=0.6):
    return [
        {"type": "predicate", "name": "is_tech_role", "probability": tech},
        {"type": "score", "name": "fit", "score": fit, "confidence": fit_confidence,
         "probabilities": [{"value": i, "label": str(i), "probability": 0.2} for i in range(5)]},
        {"type": "predicate", "name": "is_posting", "probability": posting},
    ]


def _resp(status, body=None, headers=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body
    r.text = str(body)
    return r


def _ok(answers=None):
    return _resp(200, {"model": "jev-1.14.0", "answers": answers if answers is not None else _answers(),
                       "usage": {"input_tokens": 3900, "output_tokens": 40, "total_tokens": 3940}})


def _client():
    return DecisionClient("http://llm:4000/v1", "sk-test", JEV, timeout=7)


def _mock(monkeypatch, response):
    post = Mock(return_value=response)
    monkeypatch.setattr("llm.requests.post", post)
    return post


# --- request -----------------------------------------------------------------

def test_request_goes_to_the_decisions_route_with_the_configured_model(monkeypatch):
    post = _mock(monkeypatch, _ok())
    _client().score("JOB TEXT", "CV TEXT")
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "http://llm:4000/v1/decisions"
    assert kwargs["json"]["model"] == JEV
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["timeout"] == 7


def test_input_carries_the_posting_before_the_cv(monkeypatch):
    post = _mock(monkeypatch, _ok())
    _client().score("JOB TEXT", "CV TEXT")
    text = post.call_args.kwargs["json"]["input"]
    assert "JOB TEXT" in text and "CV TEXT" in text
    assert text.index("JOB TEXT") < text.index("CV TEXT")


def test_asks_the_three_questions(monkeypatch):
    post = _mock(monkeypatch, _ok())
    _client().score("JOB TEXT", "CV TEXT")
    questions = {q["name"]: q for q in post.call_args.kwargs["json"]["questions"]}
    assert questions["is_tech_role"]["type"] == "predicate"
    assert questions["is_posting"]["type"] == "predicate"
    assert questions["fit"]["type"] == "score"
    assert len(questions["fit"]["levels"]) == 5


# --- mapping -----------------------------------------------------------------

def test_fit_maps_onto_zero_to_one_hundred(monkeypatch):
    _mock(monkeypatch, _ok(_answers(fit=3.0)))
    result = _client().score("JOB", "CV")
    assert result.ok
    assert result.data.score == 75
    assert result.data.posting_usable is True
    assert result.model == "jev-1.14.0"
    assert result.usage["input_tokens"] == 3900


def test_fit_rounds_to_the_nearest_point(monkeypatch):
    _mock(monkeypatch, _ok(_answers(fit=3.07)))     # 76.75
    assert _client().score("JOB", "CV").data.score == 77


def test_jev_writes_no_reasoning_or_gap_lists(monkeypatch):
    _mock(monkeypatch, _ok())
    data = _client().score("JOB", "CV").data
    assert data.reasoning == "" and data.missing_confirmed == [] and data.missing_unknown == []


def test_a_non_technology_role_is_capped_at_forty(monkeypatch):
    _mock(monkeypatch, _ok(_answers(tech=0.2, fit=4.0)))
    assert _client().score("JOB", "CV").data.score == 40


def test_the_cap_never_raises_a_low_score(monkeypatch):
    _mock(monkeypatch, _ok(_answers(tech=0.2, fit=1.0)))
    assert _client().score("JOB", "CV").data.score == 25


def test_text_that_is_not_a_posting_is_unusable(monkeypatch):
    _mock(monkeypatch, _ok(_answers(posting=0.1)))
    data = _client().score("JOB", "CV").data
    assert data.posting_usable is False and data.score == 0


def test_detail_carries_the_raw_answers_for_the_log(monkeypatch):
    _mock(monkeypatch, _ok(_answers(tech=0.95, fit=3.0, fit_confidence=0.6)))
    detail = _client().score("JOB", "CV").detail
    assert "tech=0.95" in detail and "fit=3.00" in detail and "conf=0.60" in detail


# --- failures ----------------------------------------------------------------

def test_429_is_transient_and_honours_retry_after(monkeypatch):
    _mock(monkeypatch, _resp(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "12"}))
    result = _client().score("JOB", "CV")
    assert result.kind == "transient" and result.retry_after == 12.0
    assert "slow down" in result.error


def test_401_is_unavailable(monkeypatch):
    _mock(monkeypatch, _resp(401, {"error": {"message": "bad key"}}))
    assert _client().score("JOB", "CV").kind == "unavailable"


def test_400_is_invalid(monkeypatch):
    _mock(monkeypatch, _resp(400, {"error": {"message": "Invalid Decisions request"}}))
    assert _client().score("JOB", "CV").kind == "invalid"


def test_connection_error_is_unavailable(monkeypatch):
    monkeypatch.setattr("llm.requests.post", Mock(side_effect=requests.ConnectionError("refused")))
    assert _client().score("JOB", "CV").kind == "unavailable"


def test_timeout_is_transient(monkeypatch):
    monkeypatch.setattr("llm.requests.post", Mock(side_effect=requests.Timeout("slow")))
    assert _client().score("JOB", "CV").kind == "transient"


def test_a_missing_answer_is_invalid(monkeypatch):
    _mock(monkeypatch, _ok(_answers()[:2]))
    result = _client().score("JOB", "CV")
    assert result.kind == "invalid" and "is_posting" in result.error


def test_a_refusal_is_invalid(monkeypatch):
    answers = _answers()
    answers[1] = {"type": "refusal", "name": "fit"}
    _mock(monkeypatch, _ok(answers))
    result = _client().score("JOB", "CV")
    assert result.kind == "invalid" and "fit" in result.error


def test_a_body_without_answers_is_invalid(monkeypatch):
    _mock(monkeypatch, _resp(200, {"model": "jev"}))
    assert _client().score("JOB", "CV").kind == "invalid"
