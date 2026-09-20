import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import trafilatura

log = logging.getLogger(__name__)

MIN_DESCRIPTION_CHARS = 300
DESCRIPTION_CAP = 12000
FETCH_TIMEOUT = 15
MAX_PAGE_CHARS = 2_000_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS_HTML = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
_HEADERS_JSON = {"User-Agent": USER_AGENT, "Accept": "application/json"}


@dataclass(frozen=True)
class FetchResult:
    text: str | None
    host: str
    strategy: str
    kind: str  # "ok" | "transient" | "permanent"
    error: str | None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


# --- text helpers ------------------------------------------------------------

_BLOCK_CLOSE = re.compile(r"</(p|div|li|h[1-6]|tr|td|th|ul|ol|section|article|blockquote)\s*>|<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
# Line-anchored: html_to_text and trafilatura emit one block per line, so section
# headings sit at line start. Unanchored keywords matched EEO/"minimum wage" boilerplate.
_REQUIREMENTS = re.compile(
    r"^\s*(?:requirements?|qualifications?|what you.ll need|what we.re looking for|must[- ]haves?"
    r"|minimum (?:qualifications|requirements)|basic qualifications|you bring)\b",
    re.I | re.M,
)


def html_to_text(html: str | None) -> str:
    """Rough HTML → plain text: entities unescaped (twice — Greenhouse double-escapes),
    block closers and <br> become newlines, tags dropped, whitespace normalised."""
    if not html:
        return ""
    # Unescape twice: Greenhouse returns HTML that is itself entity-escaped, and a
    # second pass over already-plain text is a no-op.
    s = html_lib.unescape(html_lib.unescape(html))
    s = _BLOCK_CLOSE.sub("\n", s)
    s = _TAG.sub("", s)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def has_requirements(text: str) -> bool:
    return bool(_REQUIREMENTS.search(text))


def classify_status(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 429 or status >= 500:
        return "transient"
    return "permanent"


# --- URL matching ------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PATTERNS = [
    ("greenhouse", re.compile(r"^https?://(?:job-)?boards\.greenhouse\.io/(?P<board>[^/?#]+)/jobs/(?P<job_id>\d+)")),
    ("lever", re.compile(rf"^https?://jobs\.lever\.co/(?P<company>[^/?#]+)/(?P<uuid>{_UUID})")),
    ("ashby", re.compile(rf"^https?://jobs\.ashbyhq\.com/(?P<company>[^/?#]+)/(?P<uuid>{_UUID})")),
    ("smartrecruiters", re.compile(r"^https?://jobs\.smartrecruiters\.com/(?P<company>[^/?#]+)/(?P<posting_id>\d+)")),
    ("workday", re.compile(
        r"^https?://(?P<tenant>[^./]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
        r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)/job/(?P<path>[^?#]+)")),
]


def match_ats(url: str) -> tuple[str, dict] | None:
    for name, pattern in _PATTERNS:
        m = pattern.match(url)
        if m:
            return name, m.groupdict()
    return None


# --- ATS API handlers --------------------------------------------------------

def _get_json(url: str) -> tuple[str, dict | None, str | None]:
    """GET a JSON endpoint. Returns (kind, body, error)."""
    try:
        resp = requests.get(url, headers=_HEADERS_JSON, timeout=FETCH_TIMEOUT)
    except requests.RequestException as e:
        return "transient", None, f"{type(e).__name__}: {e}"
    kind = classify_status(resp.status_code)
    if kind != "ok":
        return kind, None, f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return "permanent", None, "non-JSON response"
    if not isinstance(body, dict):
        return "permanent", None, "unexpected JSON shape"
    return "ok", body, None


def _finish(text: str, host: str, strategy: str) -> FetchResult:
    text = text.strip()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return FetchResult(None, host, strategy, "permanent", f"too short ({len(text)} chars)")
    return FetchResult(text, host, strategy, "ok", None)


def _greenhouse(board: str, job_id: str, strategy: str = "greenhouse") -> FetchResult:
    host = "boards-api.greenhouse.io"
    kind, body, err = _get_json(f"https://{host}/v1/boards/{board}/jobs/{job_id}")
    if kind != "ok":
        return FetchResult(None, host, strategy, kind, err)
    return _finish(html_to_text(body.get("content", "")), host, strategy)


def _lever(company: str, uuid: str) -> FetchResult:
    host = "api.lever.co"
    kind, body, err = _get_json(f"https://{host}/v0/postings/{company}/{uuid}")
    if kind != "ok":
        return FetchResult(None, host, "lever", kind, err)
    parts = [body.get("descriptionPlain", "")]
    for section in body.get("lists", []) or []:
        parts.append(section.get("text", ""))
        parts.append(html_to_text(section.get("content", "")))
    parts.append(body.get("additionalPlain", ""))
    return _finish("\n".join(p for p in parts if p), host, "lever")


def _ashby(company: str, uuid: str) -> FetchResult:
    host = "api.ashbyhq.com"
    kind, body, err = _get_json(f"https://{host}/posting-api/job-board/{company}")
    if kind != "ok":
        return FetchResult(None, host, "ashby", kind, err)
    for job in body.get("jobs", []) or []:
        if uuid in (job.get("jobUrl") or ""):
            return _finish(job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", "")), host, "ashby")
    return FetchResult(None, host, "ashby", "permanent", f"posting {uuid} not on board {company}")


def _smartrecruiters(company: str, posting_id: str) -> FetchResult:
    host = "api.smartrecruiters.com"
    kind, body, err = _get_json(f"https://{host}/v1/companies/{company}/postings/{posting_id}")
    if kind != "ok":
        return FetchResult(None, host, "smartrecruiters", kind, err)
    sections = (body.get("jobAd") or {}).get("sections") or {}
    parts = [html_to_text((sections.get(k) or {}).get("text", ""))
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    return _finish("\n".join(p for p in parts if p), host, "smartrecruiters")


def _workday(tenant: str, wd: str, site: str, path: str) -> FetchResult:
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    kind, body, err = _get_json(f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}")
    if kind != "ok":
        return FetchResult(None, host, "workday", kind, err)
    return _finish(html_to_text((body.get("jobPostingInfo") or {}).get("jobDescription", "")), host, "workday")


_HANDLERS = {
    "greenhouse": lambda p: _greenhouse(p["board"], p["job_id"]),
    "lever": lambda p: _lever(p["company"], p["uuid"]),
    "ashby": lambda p: _ashby(p["company"], p["uuid"]),
    "smartrecruiters": lambda p: _smartrecruiters(p["company"], p["posting_id"]),
    "workday": lambda p: _workday(p["tenant"], p["wd"], p["site"], p["path"]),
}


def fetch_via_api(name: str, params: dict) -> FetchResult:
    return _HANDLERS[name](params)


# --- orchestration -----------------------------------------------------------

_GH_EMBED_BOARD = re.compile(r"greenhouse\.io/embed/(?:job_board|job_app)[^\"']*?[?&]for=([A-Za-z0-9_-]+)")
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")


def fetch_description(url: str) -> FetchResult:
    """Apply URL → description text. API handlers first, page extraction last."""
    matched = match_ats(url)
    if matched:
        return fetch_via_api(*matched)

    host = urlparse(url).netloc
    try:
        resp = requests.get(url, headers=_HEADERS_HTML, timeout=FETCH_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return FetchResult(None, host, "none", "transient", f"{type(e).__name__}: {e}")
    kind = classify_status(resp.status_code)
    if kind != "ok":
        return FetchResult(None, host, "none", kind, f"HTTP {resp.status_code}")

    final_url = resp.url or url
    final_host = urlparse(final_url).netloc or host
    matched = match_ats(final_url)
    if matched:
        return fetch_via_api(*matched)

    # Only parse HTML: a PDF or JSON body is not a job page. A missing header is treated as HTML.
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and not ctype.startswith(("text/html", "application/xhtml")):
        return FetchResult(None, final_host, "none", "permanent", f"not HTML ({ctype.split(';')[0]})")
    html = (resp.text or "")[:MAX_PAGE_CHARS]

    jid = _GH_JID.search(final_url) or _GH_JID.search(url)
    board = _GH_EMBED_BOARD.search(html)
    if jid and board:
        return _greenhouse(board.group(1), jid.group(1), strategy="greenhouse-embed")

    extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
    if not extracted:
        return FetchResult(None, final_host, "page", "permanent", "no extractable text")
    return _finish(extracted, final_host, "page")
