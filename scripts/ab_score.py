"""Re-score stored evaluations under different request payloads and compare.

Answers questions of the form "if I change X, do the scores still hold?" — a prompt edit,
a different model alias, or turning a reasoning model's thinking off. It re-scores postings
that already have a score, so every row carries its own ground truth: what the live system
decided, next to what each variant decides now.

It reads the database read-only and never writes to it. Nothing here runs in the daemon.

Each variant is a JSON object merged into the request payload, so
`--variant '{"reasoning_effort":"none"}'` sends exactly that field alongside the usual
model/messages/temperature. `--variant '{}'` re-runs the request unchanged, which is worth
including: it shows how much the scores move on their own between two identical calls, and
a variant that moves them less than that baseline noise has not actually changed anything.

Usage (inside the container, where the endpoint and the database are both reachable):

    docker compose exec -T internship-tracker \\
        python3 /app/scripts/ab_score.py --limit 8 \\
        --variant '{}' --variant '{"reasoning_effort":"none"}'

Add --ids 165,171,177,183 to re-score specific evaluations instead of the most recent ones.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cv import MasterCV, cv_to_text  # noqa: E402
from fetcher import DESCRIPTION_CAP  # noqa: E402
from llm import ScoreResponse, _strip_fence  # noqa: E402
from prompts import SCORE_SYSTEM, score_user_message  # noqa: E402


class Row:
    """One stored evaluation plus what each variant scored it."""

    def __init__(self, ev_id, stored_score, company, role, messages):
        self.ev_id = ev_id
        self.stored_score = stored_score
        self.company = company
        self.role = role
        self.messages = messages
        self.results = []          # one Result per variant, in order

    @property
    def deltas(self):
        return [r.score - self.stored_score for r in self.results if r.score is not None]


class Result:
    def __init__(self, score=None, ms=0, reasoning_tokens=None, completion_tokens=None, error=None):
        self.score = score
        self.ms = ms
        self.reasoning_tokens = reasoning_tokens
        self.completion_tokens = completion_tokens
        self.error = error

    def cell(self):
        if self.error:
            return f"ERR:{self.error[:14]}"
        return f"{self.score:>3}"


def load_rows(db_path: str, limit: int, ids: list[int] | None) -> list[Row]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    where = "e.score IS NOT NULL AND j.description IS NOT NULL AND e.cv_snapshot IS NOT NULL"
    params: list = []
    if ids:
        where += f" AND e.id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    sql = (
        "SELECT e.id, e.score, e.cv_snapshot, j.company, j.role, j.description "
        f"FROM evaluations e JOIN jobs j ON j.id = e.job_id WHERE {where} "
        "ORDER BY e.id DESC LIMIT ?"
    )
    rows = []
    for ev_id, score, snapshot, company, role, description in db.execute(sql, [*params, limit]):
        # The snapshot, not the live CV: this reproduces the request that produced `score`.
        cv_text = cv_to_text(MasterCV.model_validate(json.loads(snapshot)))
        messages = [
            {"role": "system", "content": SCORE_SYSTEM},
            {"role": "user", "content": score_user_message(description[:DESCRIPTION_CAP], cv_text)},
        ]
        rows.append(Row(ev_id, score, company, role, messages))
    return rows


def ask(url: str, headers: dict, model: str, messages: list, overrides: dict, timeout: int) -> Result:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        **overrides,
    }
    started = time.monotonic()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return Result(error=type(e).__name__)
    ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 300:
        return Result(ms=ms, error=f"HTTP {resp.status_code}")
    body = resp.json()
    usage = body.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = ScoreResponse.model_validate(json.loads(_strip_fence(content)))
    except Exception as e:  # noqa: BLE001 — a malformed reply is a data point, not a crash
        return Result(ms=ms, error=f"{type(e).__name__}",
                      reasoning_tokens=details.get("reasoning_tokens"),
                      completion_tokens=usage.get("completion_tokens"))
    return Result(parsed.score, ms, details.get("reasoning_tokens"), usage.get("completion_tokens"))


def report(rows: list[Row], variants: list[dict]) -> None:
    labels = [json.dumps(v) if v else "{} (repeat)" for v in variants]
    width = max([len(lbl) for lbl in labels] + [8])
    header = f"{'ev':>5} {'was':>4} " + " ".join(f"{lbl:>{width}}" for lbl in labels) + "  posting"
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = " ".join(f"{r.cell():>{width}}" for r in row.results)
        posting = f"{row.company} — {row.role}"[:46]
        print(f"{row.ev_id:>5} {row.stored_score:>4} {cells}  {posting}")

    print()
    for i, label in enumerate(labels):
        results = [r.results[i] for r in rows if i < len(r.results)]
        scored = [r for r in results if r.score is not None]
        if not scored:
            print(f"{label}: every call failed")
            continue
        deltas = [abs(r.score - row.stored_score) for row, r in zip(rows, results) if r.score is not None]
        flips = sum(1 for row, r in zip(rows, results)
                    if r.score is not None and (r.score >= 60) != (row.stored_score >= 60))
        ms = sum(r.ms for r in scored) / len(scored)
        reasoning = [r.reasoning_tokens for r in scored if r.reasoning_tokens is not None]
        print(f"{label}: mean |delta| {sum(deltas) / len(deltas):.1f} pts, "
              f"max {max(deltas)} pts, {flips}/{len(scored)} crossed the 60 threshold, "
              f"{ms:.0f} ms avg" + (f", {sum(reasoning) / len(reasoning):.0f} reasoning tokens avg"
                                    if reasoning else ", no reasoning tokens reported"))
    print("\nA variant whose numbers are no better than the '{} (repeat)' row changed nothing;"
          "\nthat row is this endpoint's own run-to-run noise.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=8, help="how many recent evaluations (default 8)")
    parser.add_argument("--ids", help="comma-separated evaluation ids to use instead")
    parser.add_argument("--variant", action="append", default=[],
                        help="JSON merged into the request payload; repeatable")
    parser.add_argument("--model", help="override LLM_SCORE_MODEL")
    args = parser.parse_args()

    load_dotenv()
    data_dir = os.environ.get("DATA_DIR", "/data")
    base_url = (os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    model = args.model or os.environ.get("LLM_SCORE_MODEL")
    if not base_url or not model:
        print("LLM_BASE_URL and LLM_SCORE_MODEL must be set (they are in the server's .env)")
        return 2

    headers = {"Content-Type": "application/json"}
    if os.environ.get("LLM_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['LLM_API_KEY']}"

    variants = [json.loads(v) for v in (args.variant or ['{}'])]
    ids = [int(i) for i in args.ids.split(",")] if args.ids else None
    rows = load_rows(os.path.join(data_dir, "tracker.db"), args.limit, ids)
    if not rows:
        print("No scored evaluations with a description and a CV snapshot found.")
        return 1

    total = len(rows) * len(variants)
    print(f"Re-scoring {len(rows)} evaluation(s) under {len(variants)} variant(s) "
          f"= {total} call(s) against {model}\n")
    done = 0
    for row in rows:
        for overrides in variants:
            row.results.append(ask(f"{base_url}/chat/completions", headers, model,
                                   row.messages, overrides, timeout=180))
            done += 1
            print(f"\r  {done}/{total}", end="", flush=True)
    print("\r" + " " * 20 + "\r", end="")
    report(rows, variants)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
