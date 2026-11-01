import copy
import re
from dataclasses import dataclass, replace
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
# fit_to_page keeps adding content until the page is at least this full.
FILL_TARGET = 0.90
# A render is ~100ms; this bounds the fit loop at a few seconds in the worst case.
MAX_FIT_RENDERS = 25


@dataclass(frozen=True)
class RenderResult:
    path: str
    pages: int
    fill: float = 1.0          # share of the last page the content occupies, 0-1
    selection: dict | None = None   # what was rendered, when a fit changed it

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


def fit_to_page(cv: MasterCV, selection: dict, out_path: str, render=None,
                target: float = FILL_TARGET) -> RenderResult:
    """Render, measure, adjust, re-render until the resume is one full page.

    A fixed cap on entries and bullets cannot do this: bullets run from one line to three
    and project names from two words to a whole line, so the same count lands anywhere
    from three-quarters of a page to a page and a quarter. The page is measured instead.

    Over one page, the least relevant bullet goes first — the model's ordering is its
    relevance ranking, and projects give way before experience. Under the target, the
    candidate's own unselected bullets come back, deepening what is already shown before
    adding anything new, and any addition that would spill onto a second page is skipped
    in favour of a shorter one. Fonts and margins never change; content does.

    Returns the final render with `selection` set to what was actually rendered, so the
    caller can store it and a later re-render reproduces this exact page.
    """
    render = render or render_pdf
    sel = copy.deepcopy(selection)
    result = render(cv, sel, out_path)
    renders, on_disk_is_sel = 1, True

    while result.overflow and renders < MAX_FIT_RENDERS and _trim_one(sel):
        result = render(cv, sel, out_path)
        renders += 1

    rejected: set[tuple] = set()
    while not result.overflow and result.fill < target and renders < MAX_FIT_RENDERS:
        candidate = next((c for c in _fill_candidates(cv, sel) if c not in rejected), None)
        if candidate is None:
            break
        trial = _with_addition(sel, *candidate)
        attempt = render(cv, trial, out_path)
        renders += 1
        if attempt.overflow:
            rejected.add(candidate)          # too long; something shorter may still fit
            on_disk_is_sel = False
            continue
        sel, result, on_disk_is_sel = trial, attempt, True

    if not on_disk_is_sel:
        # The last render was a rejected trial; the file must hold what we return.
        result = render(cv, sel, out_path)
    return replace(result, selection=sel)


def _trim_one(sel: dict) -> bool:
    """Drop the least relevant bullet, or its whole entry once it is down to one.

    Projects go before experience, and within a section the last entry first. The last
    remaining experience entry is never dropped. Returns False when nothing can go.
    """
    for section in ("projects", "experience"):
        entries = sel.get(section) or []
        if not entries:
            continue
        last = entries[-1]
        if len(last["bullets"]) > 1:
            last["bullets"].pop()
            return True
        if section == "experience" and len(entries) == 1:
            return False
        entries.pop()
        return True
    return False


def _fill_candidates(cv: MasterCV, sel: dict) -> list[tuple[str, str, str]]:
    """What could be added, best first.

    The model's spares come first, in its order — what it ranked just past the cut is the
    most relevant thing not on the page. Only once those run out does the master's order
    take over: more of what is already shown, then entries that are not.
    """
    masters = {"experience": cv.experience, "projects": cv.projects}
    bullets = {section: {e.id: {b.id for b in e.bullets} for e in entries}
               for section, entries in masters.items()}
    shown = {(section, item["id"], b) for section in masters
             for item in sel.get(section) or [] for b in item["bullets"]}
    out = []
    for row in sel.get("reserve") or []:
        candidate = tuple(row)
        if len(candidate) != 3 or candidate in shown or candidate in out:
            continue
        section, entry_id, bullet_id = candidate
        if bullet_id in bullets.get(section, {}).get(entry_id, ()):   # the snapshot is user data
            out.append(candidate)
    for section in ("experience", "projects"):
        by_id = {e.id: e for e in masters[section]}
        for item in sel.get(section) or []:
            entry = by_id.get(item["id"])
            if entry is not None:
                out += [(section, entry.id, b.id) for b in entry.bullets
                        if b.id not in item["bullets"] and (section, entry.id, b.id) not in out]
    for section in ("experience", "projects"):
        shown_ids = {item["id"] for item in sel.get(section) or []}
        out += [(section, e.id, e.bullets[0].id) for e in masters[section]
                if e.id not in shown_ids and e.bullets and (section, e.id, e.bullets[0].id) not in out]
    return out


def _with_addition(sel: dict, section: str, entry_id: str, bullet_id: str) -> dict:
    trial = copy.deepcopy(sel)
    entries = trial.setdefault(section, [])
    for item in entries:
        if item["id"] == entry_id:
            item["bullets"].append(bullet_id)
            return trial
    entries.append({"id": entry_id, "bullets": [bullet_id]})
    return trial


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
