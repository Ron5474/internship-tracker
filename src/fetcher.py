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

_BLOCK_CLOSE = re.compile(r"</(p|div|li|h[1-6]|tr|ul|ol|section|article|blockquote)\s*>|<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_REQUIREMENTS = re.compile(
    r"\b(requirements?|qualifications?|what you.ll need|what we.re looking for|must[- ]haves?|minimum"
    r"|basic qualifications|you have|you bring)\b",
    re.I,
)


def html_to_text(html: str) -> str:
    """Rough HTML → plain text: entities unescaped (twice — Greenhouse double-escapes),
    block closers and <br> become newlines, tags dropped, whitespace normalised."""
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

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
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
