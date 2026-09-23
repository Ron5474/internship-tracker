import json
import re
import time
from dataclasses import dataclass

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prompts import SCORE_SYSTEM, TAILOR_SYSTEM, reask_message, score_user_message, tailor_user_message

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class ScoreResponse(BaseModel):
    """Strict on purpose: a score of `false`, "82" or 140 is a malformed reply, not a datum.
    Coercing it could silently suppress a real match; rejecting it triggers the re-ask."""
    model_config = ConfigDict(strict=True, extra="ignore")

    score: int = Field(ge=0, le=100)
    reasoning: str
    missing_confirmed: list[str]
    missing_unknown: list[str]
    posting_usable: bool = True   # false: the text was not a job posting (login wall, error page, ...)


class TailorEntry(BaseModel):
    """Strict: a bullet must be a string ID. An object or a rewritten sentence is a failed reply,
    not something to coerce — rendering unvalidated prose is exactly what this design forbids."""
    model_config = ConfigDict(strict=True, extra="ignore")

    id: str
    bullets: list[str] = Field(default_factory=list)


class TailorResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    experience: list[TailorEntry] = Field(default_factory=list)
    projects: list[TailorEntry] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)


@dataclass(frozen=True)
class LLMResult:
    kind: str  # "ok" | "transient" | "unavailable" | "invalid"
    data: ScoreResponse | TailorResponse | None
    error: str | None
    retry_after: float | None
    model: str | None
    usage: dict | None
    ms: int

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def classify_status(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status in (408, 429) or status >= 500:
        return "transient"
    if status in (401, 403, 404):
        return "unavailable"   # bad key, blocked, or unknown model: config, not this item
    return "invalid"


def _sum_usage(a: dict | None, b: dict | None) -> dict | None:
    """Token usage across both requests of a re-ask: the second call is billed too.
    Numeric counters (prompt/completion/total) are summed, a key missing on one side counts as 0;
    non-numeric extras (nested *_details) are dropped rather than guessed at."""
    if not a or not b:
        return a or b
    out = {}
    for k in sorted(a.keys() | b.keys()):
        x, y = a.get(k), b.get(k)
        if isinstance(x, (int, float)) or isinstance(y, (int, float)):
            out[k] = (x if isinstance(x, (int, float)) else 0) + (y if isinstance(y, (int, float)) else 0)
    return out


def _strip_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def _error_message(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
    except ValueError:
        pass
    return (resp.text or "")[:200]


class LLMClient:
    def __init__(self, base_url: str, api_key: str | None, model: str, timeout: int = 120) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._model = model
        self._timeout = timeout

    # -- public -------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._model

    def score(self, description: str, cv_text: str) -> LLMResult:
        return self._ask([
            {"role": "system", "content": SCORE_SYSTEM},
            {"role": "user", "content": score_user_message(description, cv_text)},
        ], ScoreResponse)

    def tailor(self, description: str, cv_id_text: str, max_bullets: int) -> LLMResult:
        return self._ask([
            {"role": "system", "content": TAILOR_SYSTEM},
            {"role": "user", "content": tailor_user_message(description, cv_id_text, max_bullets)},
        ], TailorResponse)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _chat(self, messages: list[dict]):
        """Returns (kind, content, error, retry_after, model, usage)."""
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout)
        except requests.ConnectionError as e:
            return "unavailable", None, f"{type(e).__name__}: {e}", None, None, None
        except requests.RequestException as e:
            return "transient", None, f"{type(e).__name__}: {e}", None, None, None

        kind = classify_status(resp.status_code)
        if kind != "ok":
            retry_after = None
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            return kind, None, f"HTTP {resp.status_code}: {_error_message(resp)}", retry_after, None, None

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return "invalid", None, f"unexpected response shape: {type(e).__name__}", None, None, None
        return "ok", content or "", None, None, body.get("model"), body.get("usage")

    @staticmethod
    def _parse(content: str, model_cls) -> tuple[BaseModel | None, str | None]:
        try:
            return model_cls.model_validate(json.loads(_strip_fence(content))), None
        except (ValueError, ValidationError) as e:  # json.JSONDecodeError is a ValueError
            return None, str(e)[:300]

    def _ask(self, messages: list[dict], model_cls) -> LLMResult:
        """One call, one re-ask on a schema failure. Shared by score and tailor."""
        started = time.monotonic()
        kind, content, error, retry_after, model, usage = self._chat(messages)
        if kind == "ok":
            parsed, perr = self._parse(content, model_cls)
            if parsed is None:
                # One re-ask, carrying the bad reply and what was wrong with it.
                messages = messages + [{"role": "assistant", "content": content},
                                       {"role": "user", "content": reask_message(perr)}]
                kind, content, error, retry_after, model2, usage2 = self._chat(messages)
                model, usage = model2 or model, _sum_usage(usage, usage2)
                if kind == "ok":
                    parsed, perr = self._parse(content, model_cls)
                    if parsed is None:
                        kind, error = "invalid", f"schema validation failed after re-ask: {perr}"
            if kind == "ok":
                return LLMResult("ok", parsed, None, None, model, usage, self._ms(started))
        return LLMResult(kind, None, error, retry_after, model, usage, self._ms(started))
