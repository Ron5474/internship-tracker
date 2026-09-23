import json
from unittest.mock import Mock, patch

import requests

from discord_client import (
    MAX_CONTENT,
    DeliveryResult,
    cap_content,
    format_link_only,
    send_message,
)

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def _resp(status, body=None, headers=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {}
    r.text = "" if body is None else str(body)
    return r


# --- format_link_only -------------------------------------------------------

def test_format_link_only_basic():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://stripe.com/j?gh_jid=1")
    assert msg == "🆕 **Stripe** — SWE Intern\n📍 SF\n🔗 https://stripe.com/j?gh_jid=1"


def test_format_link_only_with_note():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://x", note="couldn't read the description")
    assert msg.startswith("🆕 **Stripe** — SWE Intern (couldn't read the description)")


# --- cap_content -----------------------------------------------------------

def test_cap_content_leaves_short_message_alone():
    assert cap_content("head", ["⚠️ Gaps: a, b"], "tail") == "head\n⚠️ Gaps: a, b\ntail"


def test_cap_content_truncates_lists_before_tail():
    long_list = "⚠️ Gaps: " + ", ".join(f"req{i}" for i in range(600))
    out = cap_content("head", [long_list], "tail")
    assert len(out) <= MAX_CONTENT
    assert out.endswith("tail")
    assert "…" in out


def test_cap_content_truncates_tail_when_lists_already_minimal():
    # Priority flipped from the pre-Task-5 behavior: the tail carries the resume notices and must
    # never be what gets dropped, even in this pathological case where an oversized tail alone
    # consumes the whole budget and the header has to give way instead. (Real notices are short
    # fixed strings, so a header never actually vanishes in practice.)
    out = cap_content("head", [], "x" * 5000)
    assert len(out) <= MAX_CONTENT
    assert "head" not in out
    assert out.endswith("…")


# --- send_message ----------------------------------------------------------

def test_send_uses_wait_true_and_succeeds_on_200_with_body():
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi")
    assert result.ok
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    assert post.call_args.kwargs["json"] == {"content": "hi"}


def test_send_204_is_not_success():
    # wait=true should never yield 204; if it does, Discord did not confirm the message.
    with patch("discord_client.requests.post", return_value=_resp(204)):
        result = send_message(WEBHOOK, "hi")
    assert not result.ok
    assert result.kind == "transient"


def test_send_429_is_transient_with_retry_after():
    with patch("discord_client.requests.post", return_value=_resp(429, {}, {"Retry-After": "7"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert result.retry_after == 7.0


def test_send_200_without_message_body_is_transient():
    # Discord only confirms a message by returning it; a bare 200 proves nothing.
    with patch("discord_client.requests.post", return_value=_resp(200, {})):
        assert send_message(WEBHOOK, "hi").kind == "transient"
    broken = _resp(200)
    broken.json.side_effect = ValueError("not json")
    with patch("discord_client.requests.post", return_value=broken):
        assert send_message(WEBHOOK, "hi").kind == "transient"


def test_send_429_reads_retry_after_from_json_body():
    with patch("discord_client.requests.post", return_value=_resp(429, {"retry_after": 3.5})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert result.retry_after == 3.5


def test_send_429_with_non_dict_body_has_no_retry_after():
    with patch("discord_client.requests.post", return_value=_resp(429, ["not", "a", "dict"])):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert result.retry_after is None


def test_send_5xx_is_transient():
    with patch("discord_client.requests.post", return_value=_resp(503)):
        assert send_message(WEBHOOK, "hi").kind == "transient"


def test_send_connection_error_is_transient():
    with patch("discord_client.requests.post", side_effect=requests.ConnectionError("down")):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert "down" in result.error


def test_send_404_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(404)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_401_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(401)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_400_is_invalid():
    with patch("discord_client.requests.post", return_value=_resp(400, {"message": "bad"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "invalid"
    assert "bad" in result.error


def test_send_with_pdf_uses_multipart(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi", pdf_path=str(pdf))
    assert result.ok
    kwargs = post.call_args.kwargs
    assert "json" not in kwargs
    assert kwargs["data"] == {"payload_json": '{"content": "hi"}'}
    filename, _fh, ctype = kwargs["files"]["files[0]"]
    assert filename == "r.pdf"
    assert ctype == "application/pdf"


def test_delivery_result_ok_property():
    assert DeliveryResult("ok", None, None).ok
    assert not DeliveryResult("transient", None, None).ok


# --- format_match -----------------------------------------------------------

from discord_client import format_match


def test_format_match_full():
    msg = format_match("Stripe", "SWE Intern", "SF", "https://s/j?gh_jid=1", 82, "Strong Python match.",
                       ["Kubernetes"], ["work authorization"], matched=True)
    assert msg == ("🎯 82% — **Stripe** — SWE Intern\n📍 SF\n🔗 https://s/j?gh_jid=1\n✅ Why: Strong Python match.\n"
                   "⚠️ Gaps: Kubernetes\n❓ Not on CV: work authorization")


def test_format_match_below_threshold_icon_and_omits_empty_lists():
    msg = format_match("Stripe", "SWE Intern", "SF", "https://s", 41, "Different discipline.", [], [], matched=False)
    assert msg.startswith("📉 41% — **Stripe**")
    assert "Gaps" not in msg and "Not on CV" not in msg


def test_format_match_caps_at_2000_truncating_lists_first():
    gaps = [f"requirement number {i}" for i in range(300)]
    msg = format_match("S", "R", "L", "https://s", 70, "Why.", gaps, [], matched=True)
    assert len(msg) <= MAX_CONTENT and "✅ Why: Why." in msg and "…" in msg


# --- resume notices ----------------------------------------------------------

OVERFLOW_NOTE = "📄 Resume ran over one page — trim before sending"
NO_RESUME_NOTE = "⚠️ Couldn't generate resume — apply with your master CV."


def test_match_message_has_no_notes_by_default():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True)
    assert OVERFLOW_NOTE not in msg and NO_RESUME_NOTE not in msg


def test_overflow_note_appended():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True, overflow=True)
    assert msg.rstrip().endswith(OVERFLOW_NOTE)


def test_missing_resume_note_appended():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True, resume_missing=True)
    assert NO_RESUME_NOTE in msg


def test_notes_survive_an_oversized_gap_list():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why",
                       ["a very long requirement " * 20] * 30, [], matched=True,
                       overflow=True, resume_missing=True)
    assert len(msg) <= 2000
    assert OVERFLOW_NOTE in msg and NO_RESUME_NOTE in msg


def test_notes_survive_an_oversized_reasoning():
    # Nothing bounds reasoning: the prompt asks for under 400 characters, the schema does not.
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "r" * 2100, ["gap"], ["unknown"],
                       matched=True, overflow=True, resume_missing=True)
    assert len(msg) <= 2000
    assert msg.startswith("🎯 82% — **Stripe** — SWE")
    assert OVERFLOW_NOTE in msg and NO_RESUME_NOTE in msg


def test_lists_are_truncated_before_the_reasoning_is():
    # Spec order: gaps and unknowns give way first, the reasoning second. The inputs must actually
    # overflow — a 1,900-character reasoning plus one short gap line still fits in 2,000 and would
    # prove nothing. Assert the relationship, not a character count.
    gaps = ["a confirmed gap that is quite wordy"] * 20      # ~750 characters once joined
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "r" * 1500, gaps, [], matched=True)
    assert len(msg) <= 2000
    assert "✅ Why: " + "r" * 1500 in msg                     # the reasoning survives intact
    assert msg.count("a confirmed gap") < len(gaps)          # the gaps line did not
    assert msg.rstrip().endswith("…")


def test_cap_content_never_exceeds_the_limit_for_any_input():
    from discord_client import cap_content
    assert len(cap_content("head", [], tail="x" * 5000)) <= 2000
    assert len(cap_content("h" * 5000, ["y" * 5000], tail="t" * 5000, body="b" * 5000)) <= 2000


def test_a_short_message_is_untouched():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "short why", ["g"], ["u"], matched=True)
    assert msg == ("🎯 82% — **Stripe** — SWE\n📍 SF\n🔗 https://x\n"
                   "✅ Why: short why\n⚠️ Gaps: g\n❓ Not on CV: u")


def test_send_message_with_a_missing_pdf_reports_an_attachment_failure(tmp_path):
    result = send_message("https://d/x", "hi", str(tmp_path / "gone.pdf"))
    assert result.kind == "attachment" and "gone.pdf" in result.error


def test_send_message_with_an_unreadable_pdf_reports_an_attachment_failure(monkeypatch, tmp_path):
    # Not chmod(0o000): CI runs as root, which reads it anyway, and the test would fall through
    # to a real HTTP request. Shadow the module's `open` instead — Python resolves module globals
    # before builtins, so this affects discord_client only.
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(b"%PDF stub")

    def denied(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("discord_client.open", denied, raising=False)
    result = send_message("https://d/x", "hi", str(pdf))
    assert result.kind == "attachment"      # the file exists; opening it is what fails


def test_attachment_failure_makes_no_request(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("discord_client.requests.post", lambda *a, **k: calls.append(1))
    send_message("https://d/x", "hi", str(tmp_path / "gone.pdf"))
    assert calls == []


def test_send_message_attaches_the_pdf(monkeypatch, tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7 stub")
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return _resp(200, {"id": "1"})          # existing helper in this file

    monkeypatch.setattr("discord_client.requests.post", fake_post)
    assert send_message("https://d/x", "hi", str(pdf)).ok
    assert seen["params"] == {"wait": "true"}
    assert "files[0]" in seen["files"]
    assert json.loads(seen["data"]["payload_json"])["content"] == "hi"
