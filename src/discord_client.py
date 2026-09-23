import json
from dataclasses import dataclass
from pathlib import Path

import requests

MAX_CONTENT = 2000
_ELLIPSIS = "…"


@dataclass(frozen=True)
class DeliveryResult:
    kind: str  # "ok" | "transient" | "gone" | "invalid"
    retry_after: float | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def format_link_only(company: str, role: str, location: str, url: str, note: str | None = None) -> str:
    head = f"🆕 **{company}** — {role}"
    if note:
        head += f" ({note})"
    return f"{head}\n📍 {location}\n🔗 {url}"


def format_match(
    company: str, role: str, location: str, url: str,
    score: int, reasoning: str, missing_confirmed: list[str], missing_unknown: list[str],
    matched: bool,
) -> str:
    icon = "🎯" if matched else "📉"
    header = f"{icon} {score}% — **{company}** — {role}\n📍 {location}\n🔗 {url}"
    if reasoning:
        header += f"\n✅ Why: {reasoning}"
    lists = []
    if missing_confirmed:
        lists.append("⚠️ Gaps: " + ", ".join(missing_confirmed))
    if missing_unknown:
        lists.append("❓ Not on CV: " + ", ".join(missing_unknown))
    return cap_content(header, lists)


def cap_content(header: str, lists: list[str], tail: str = "") -> str:
    """Join header + list lines + tail under MAX_CONTENT.

    List lines (gaps, unknowns) are truncated first, then the tail (reasoning),
    so an oversized list never produces a request Discord will reject forever.
    """
    parts = [header, *lists] + ([tail] if tail else [])
    budget = MAX_CONTENT - (len(parts) - 1)  # newlines
    fixed = len(header)
    remaining = budget - fixed - (len(tail) if tail else 0)
    capped_lists = []
    for line in lists:
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[: max(remaining - 1, 0)] + _ELLIPSIS
        capped_lists.append(line)
        remaining -= len(line)
    out = "\n".join([header, *capped_lists] + ([tail] if tail else []))
    if len(out) > MAX_CONTENT:
        out = out[: MAX_CONTENT - 1] + _ELLIPSIS
    return out


def send_message(webhook_url: str, content: str, pdf_path: str | None = None) -> DeliveryResult:
    """POST to a Discord webhook with ?wait=true. Success is 200 with a message body only."""
    try:
        if pdf_path:
            with open(pdf_path, "rb") as fh:
                resp = requests.post(
                    webhook_url,
                    params={"wait": "true"},
                    data={"payload_json": json.dumps({"content": content})},
                    files={"files[0]": (Path(pdf_path).name, fh, "application/pdf")},
                    timeout=30,
                )
        else:
            resp = requests.post(webhook_url, params={"wait": "true"}, json={"content": content}, timeout=10)
    except requests.RequestException as e:
        return DeliveryResult("transient", None, str(e))

    status = resp.status_code
    if status == 200:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("id"):
            return DeliveryResult("ok", None, None)
        return DeliveryResult("transient", None, "200 without message body")
    if status == 429:
        retry_after = _retry_after(resp)
        return DeliveryResult("transient", retry_after, "rate limited")
    if status in (401, 404):
        return DeliveryResult("gone", None, f"webhook returned {status}")
    if status >= 500 or status == 204:
        return DeliveryResult("transient", None, f"unconfirmed: HTTP {status}")
    return DeliveryResult("invalid", None, f"HTTP {status}: {resp.text[:200]}")


def _retry_after(resp) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        try:
            body = resp.json()
        except ValueError:
            body = None
        raw = body.get("retry_after") if isinstance(body, dict) else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
