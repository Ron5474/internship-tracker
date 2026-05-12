import re


def parse_new_rows(patch: str) -> list[dict]:
    rows = []
    for line in patch.splitlines():
        if not line.startswith("+|"):
            continue
        line = line[1:]  # strip leading +
        if re.match(r"^\|\s*[-:]+\s*\|", line):
            continue  # separator row like | --- | --- |
        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]  # drop empty strings from leading/trailing |
        if len(cols) < 4:
            continue
        company = _extract_company(cols[0])
        role = _strip_html(cols[1])
        location = _strip_html(cols[2])
        url = _extract_url(cols[3])
        if not company or not role:
            continue
        # skip closed positions
        if role.startswith("🔒"):
            continue
        # skip continuation rows (multiple locations for same company)
        if company == "↳":
            continue
        rows.append({"company": company, "role": role, "location": location, "url": url})
    return rows


def filter_by_keywords(rows: list[dict], keywords: list[str]) -> list[dict]:
    lower_keywords = [k.lower() for k in keywords]
    return [r for r in rows if any(k in r["role"].lower() for k in lower_keywords)]


def _extract_company(text: str) -> str:
    match = re.search(r"\*\*\[(.+?)\]", text)
    if match:
        return match.group(1)
    return re.sub(r"[*\[\]()]+", "", text).strip()


def _extract_url(text: str) -> str:
    match = re.search(r'href="([^"]+)"', text)
    if match:
        return match.group(1)
    match = re.search(r"\(([^)]+)\)", text)
    if match:
        return match.group(1)
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
