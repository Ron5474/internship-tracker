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


@dataclass(frozen=True)
class RenderResult:
    path: str
    pages: int

    @property
    def overflow(self) -> bool:
        return self.pages > 1


def output_path(output_dir: str, user_id: str, job_id: int, company: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company or "").strip("-").lower()[:40] or "job"
    return str(Path(output_dir) / user_id / f"{job_id}-{slug}.pdf")


def _contact_line(cv: MasterCV) -> str:
    c = cv.contact
    parts = [c.email, c.phone, c.location, c.linkedin, c.github]
    return " · ".join(p for p in parts if p)


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
        "contact_line": _contact_line(cv),
        "summary": cv.summary,
        "education": cv.education,                       # always the master's, in full
        "experience": _blocks(cv.experience, selection.get("experience", []),
                              lambda e: {"company": e.company, "title": e.title,
                                         "dates": e.dates, "location": e.location}),
        "projects": _blocks(cv.projects, selection.get("projects", []),
                            # link and demo are the whole point of a project entry on a resume.
                            lambda p: {"name": p.name, "tech": p.tech, "dates": p.dates,
                                       "link": p.link, "demo": p.demo}),
        "skills": selection.get("skills", {}),
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
    return RenderResult(out_path, len(document.pages))
