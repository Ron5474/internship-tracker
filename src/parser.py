import re
from urllib.parse import urlparse, urlunparse


def url_key(url: str) -> str:
    """Strip query string and fragment for stable deduplication."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def parse_sections(content: str) -> dict[str, list[dict]]:
    """Split README into {normalized_section_name: [rows]}."""
    sections: dict[str, list[dict]] = {}
    chunks = re.split(r"\n(?=## )", content)
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines or not lines[0].startswith("## "):
            continue
        name = _normalize_section(lines[0])
        rows = _parse_table_rows("\n".join(lines[1:]))
        sections[name] = rows
    return sections


def find_new_rows(
    sections: dict[str, list[dict]],
    known_urls: set[str],
    target_sections: list[str],
) -> list[dict]:
    new_rows = []
    for section_name, rows in sections.items():
        if not any(t in section_name for t in target_sections):
            continue
        for row in rows:
            if url_key(row["url"]) not in known_urls:
                new_rows.append(row)
    return new_rows


def _normalize_section(heading: str) -> str:
    heading = re.sub(r"^##\s+", "", heading)
    heading = re.sub(r"[^\x00-\x7F]+", "", heading)
    return heading.lower().strip()


def _parse_table_rows(html: str) -> list[dict]:
    rows = []
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        row = _parse_tr_block(match.group(1))
        if row:
            rows.append(row)
    return rows


def _parse_tr_block(tr_inner: str) -> dict | None:
    tds = re.findall(r"<td>(.*?)</td>", tr_inner, re.DOTALL)
    if len(tds) < 4:
        return None
    company = _extract_text(tds[0])
    role = _extract_text(tds[1])
    location = _extract_location(tds[2])
    url = _extract_url(tds[3])
    if not company or not role:
        return None
    if role.startswith("🔒"):
        return None
    if company == "↳":
        return None
    return {"company": company, "role": role, "location": location, "url": url}


def _extract_text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _extract_location(html: str) -> str:
    html = re.sub(r"<summary>.*?</summary>", "", html, flags=re.DOTALL)
    html = re.sub(r"<br\s*/?>", ", ", html)
    return _extract_text(html)


def _extract_url(html: str) -> str:
    match = re.search(r'href="([^"]+)"', html)
    return match.group(1) if match else ""
