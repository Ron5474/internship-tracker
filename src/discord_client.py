import json
from dataclasses import dataclass
from pathlib import Path

import requests

MAX_CONTENT = 2000
_ELLIPSIS = "…"

OVERFLOW_NOTE = "📄 Resume ran over one page — trim before sending"
UNDERFILL_NOTE = "📏 Resume fills only {pct}% of the page — room for another project"
NO_RESUME_NOTE = "⚠️ Couldn't generate resume — apply with your master CV."


@dataclass(frozen=True)
class DeliveryResult:
    kind: str  # "ok" | "transient" | "gone" | "invalid" | "attachment"
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
    matched: bool, overflow: bool = False, resume_missing: bool = False,
    underfill: int | None = None,
) -> str:
    icon = "🎯" if matched else "📉"
    header = f"{icon} {score}% — **{company}** — {role}\n📍 {location}\n🔗 {url}"
    body = f"✅ Why: {reasoning}" if reasoning else ""
    lists = []
    if missing_confirmed:
        lists.append("⚠️ Gaps: " + ", ".join(missing_confirmed))
    if missing_unknown:
        lists.append("❓ Not on CV: " + ", ".join(missing_unknown))

    notes = []
    if resume_missing:
        notes.append(NO_RESUME_NOTE)
    if overflow:
        notes.append(OVERFLOW_NOTE)
    elif underfill is not None:
        # Never both: a resume cannot be simultaneously too long and too short.
        notes.append(UNDERFILL_NOTE.format(pct=underfill))
    return cap_content(header, lists, tail="\n".join(notes), body=body)


def cap_content(header: str, lists: list[str], tail: str = "", body: str = "") -> str:
    """Join header + body + list lines + tail under MAX_CONTENT.

    Priority when it does not fit: the header and the tail always survive. The tail carries the
    resume notices, and the difference between "apply with this PDF" and "apply with your master
    CV" must not be what gets dropped. List lines go first, then the body (the reasoning).
    """
    if len(tail) > MAX_CONTENT:
        # The notices are short fixed strings, so this never fires today. It is here because the
        # cap is absolute: this helper must not return more than MAX_CONTENT characters for ANY
        # input, and the branch below would otherwise hand an oversized tail straight back.
        tail = tail[: MAX_CONTENT - 1] + _ELLIPSIS

    tail_cost = len(tail) + 1 if tail else 0
    if len(header) + tail_cost > MAX_CONTENT:
        # Pathological: even the header does not fit. It gives way, never the tail.
        keep = MAX_CONTENT - tail_cost - 1
        head = header[:keep] + _ELLIPSIS if keep > 0 else ""
        return "\n".join(p for p in (head, tail) if p)

    remaining = MAX_CONTENT - len(header) - tail_cost
    kept_body = ""
    if body and remaining > 2:
        kept_body = body if len(body) + 1 <= remaining else body[: remaining - 2] + _ELLIPSIS
        remaining -= len(kept_body) + 1

    kept_lists = []
    for line in lists:
        if remaining <= 2:
            break
        if len(line) + 1 > remaining:
            line = line[: remaining - 2] + _ELLIPSIS
        kept_lists.append(line)
        remaining -= len(line) + 1

    return "\n".join(p for p in (header, kept_body, *kept_lists, tail) if p)


def send_message(webhook_url: str, content: str, pdf_path: str | None = None,
                 filename: str | None = None) -> DeliveryResult:
    """POST to a Discord webhook with ?wait=true. Success is 200 with a message body only.

    `filename` is what the attachment is called in Discord, which need not match the file on
    disk: the stored name carries a job id so re-rendering overwrites the right file, while
    the download wants a name you would attach to an application.
    """
    try:
        if pdf_path:
            try:
                fh = open(pdf_path, "rb")
            except OSError as e:
                # A distinct kind, not "invalid": the message is fine, only the file is not, and
                # `invalid` is retried forever. The worker drops the attachment and sends the rest.
                return DeliveryResult("attachment", None, f"cannot read {pdf_path}: {e}")
            with fh:
                resp = requests.post(
                    webhook_url,
                    params={"wait": "true"},
                    data={"payload_json": json.dumps({"content": content})},
                    files={"files[0]": (filename or Path(pdf_path).name, fh, "application/pdf")},
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
