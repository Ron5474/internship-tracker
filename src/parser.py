import re


def parse_new_rows(patch: str) -> list[dict]:
    rows = []
    in_tr = False
    current_block: list[str] = []

    for line in patch.splitlines():
        if not line.startswith("+"):
            if in_tr:
                in_tr = False
                current_block = []
            continue
        content = line[1:]
        if content.strip() == "<tr>":
            in_tr = True
            current_block = [content]
        elif in_tr:
            current_block.append(content)
            if content.strip() == "</tr>":
                row = _parse_tr_block(current_block)
                if row:
                    rows.append(row)
                in_tr = False
                current_block = []
    return rows


def filter_by_keywords(rows: list[dict], keywords: list[str]) -> list[dict]:
    lower_keywords = [k.lower() for k in keywords]
    return [r for r in rows if any(k in r["role"].lower() for k in lower_keywords)]


def _parse_tr_block(tr_lines: list[str]) -> dict | None:
    html = "\n".join(tr_lines)
    tds = re.findall(r"<td>(.*?)</td>", html, re.DOTALL)
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
