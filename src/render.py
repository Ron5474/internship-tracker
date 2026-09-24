import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import CSS, HTML

from cv import MasterCV

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),   # CV text is data; never markup
    trim_blocks=True,
    lstrip_blocks=True,
)


# Below this share of the page, a one-page resume has enough white space that another
# project or a few more bullets would be a better use of it.
UNDERFILL_BELOW = 0.85


@dataclass(frozen=True)
class RenderResult:
    path: str
    pages: int
    fill: float = 1.0          # share of the last page the content occupies, 0-1

    @property
    def overflow(self) -> bool:
        return self.pages > 1

    @property
    def underfilled(self) -> bool:
        """One page with room to spare. Overflow's opposite, and just as worth knowing:
        a sparse resume goes out looking thin and nothing else would notice."""
        return self.pages == 1 and self.fill < UNDERFILL_BELOW


def attachment_name(cv_name: str, company: str, role: str) -> str:
    """The name the PDF arrives under in Discord.

    The file on disk keeps its job id, which is what makes a re-render overwrite the right
    file and keeps two postings from the same company apart. This is only what the download
    is called, and there it wants to read like something you would attach to an application.
    """
    def part(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")

    pieces = [p for p in (part(cv_name), "Resume", part(company), part(role)) if p]
    return "_".join(pieces)[:120] + ".pdf"


def output_path(output_dir: str, user_id: str, job_id: int, company: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company or "").strip("-").lower()[:40] or "job"
    return str(Path(output_dir) / user_id / f"{job_id}-{slug}.pdf")


def _contact_parts(cv: MasterCV) -> list[dict]:
    """The header line, as pieces the template joins with pipes.

    Email, phone and location are plain text; the profile URLs render as their names,
    because "LinkedIn" reads better on paper than the URL it points at.
    """
    c = cv.contact
    parts: list[dict] = [{"text": t} for t in (c.email, c.phone, c.location) if t]
    if c.linkedin:
        parts.append({"text": "LinkedIn", "href": c.linkedin})
    if c.github:
        parts.append({"text": "GitHub", "href": c.github})
    return parts


def _education_rows(cv: MasterCV) -> list[dict]:
    """School, then degree with its details (a GPA, usually) on one line, then dates."""
    rows = []
    for e in cv.education:
        detail = " | ".join(e.details) if e.details else ""
        rows.append({
            "school": e.school,
            "degree": f"{e.degree} | {detail}" if detail else e.degree,
            "dates": e.dates,
        })
    return rows


def _flat_skills(skills: dict) -> list[str]:
    """One comma-separated run, not a list per group.

    The group names (languages, frameworks, tools) exist so the tailor step can keep its
    selection inside a category; they are scaffolding, and printing them wastes two lines
    of a one-page resume.
    """
    seen, out = set(), []
    for group in skills.values():
        for skill in group:
            if skill not in seen:
                seen.add(skill)
                out.append(skill)
    return out


def _project_links(project) -> list[dict]:
    links = []
    if project.link:
        label = "GitHub" if "github.com" in project.link.lower() else "Link"
        links.append({"text": label, "href": project.link})
    if project.demo:
        links.append({"text": "Demo", "href": project.demo})
    return links


def _blocks(entries, chosen, extra):
    """Entries in the selection's order, carrying only the selected bullets' text."""
    by_id = {e.id: e for e in entries}
    out = []
    for item in chosen:
        entry = by_id.get(item["id"])
        if entry is None:      # validate_selection guarantees this, but the snapshot is user data
            continue
        texts = {b.id: b.text for b in entry.bullets}
        block = {"id": entry.id, "bullets": [texts[b] for b in item["bullets"] if b in texts]}
        block.update(extra(entry))
        out.append(block)
    return out


def build_context(cv: MasterCV, selection: dict) -> dict:
    return {
        "name": cv.name,
        "contact": _contact_parts(cv),
        "summary": cv.summary,
        "education": _education_rows(cv),                # always the master's, in full
        "experience": _blocks(cv.experience, selection.get("experience", []),
                              lambda e: {"company": e.company, "title": e.title,
                                         "dates": e.dates, "location": e.location}),
        "projects": _blocks(cv.projects, selection.get("projects", []),
                            # Links are the whole point of a project entry on a resume; `tech`
                            # is deliberately not rendered — the master resume this layout
                            # copies keeps that detail inside the bullets.
                            lambda p: {"name": p.name, "dates": p.dates,
                                       "links": _project_links(p)}),
        "skills": _flat_skills(selection.get("skills", {})),
    }


def build_html(cv: MasterCV, selection: dict) -> str:
    return _env.get_template("resume.html").render(**build_context(cv, selection))


def render_pdf(cv: MasterCV, selection: dict, out_path: str) -> RenderResult:
    """Snapshot + validated selection → one-page-target PDF. Overflow is reported, never trimmed."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    document = HTML(string=build_html(cv, selection)).render(
        stylesheets=[CSS(filename=str(TEMPLATE_DIR / "resume.css"))]
    )
    document.write_pdf(out_path)
    return RenderResult(out_path, len(document.pages), _fill(document.pages[-1]))


def _fill(page) -> float:
    """How much of the last page the content occupies.

    Reads WeasyPrint's laid-out box tree, which is private API — so any surprise there
    reports a full page rather than raising, since a wrong "looks sparse" note is a worse
    outcome than no note at all.
    """
    try:
        box = page._page_box
        bottom = max((c.position_y + (c.height or 0) for c in box.children), default=box.position_y)
        return max(0.0, min(1.0, (bottom - box.position_y) / box.height))
    except Exception:  # noqa: BLE001 — a measurement is not worth failing a render over
        return 1.0
