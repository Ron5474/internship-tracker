from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")


class Bullet(_Model):
    id: str
    text: str


class Contact(_Model):
    email: str
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None


class Education(_Model):
    id: str
    school: str
    degree: str
    dates: str
    details: list[str] = Field(default_factory=list)


class Experience(_Model):
    id: str
    company: str
    title: str
    dates: str
    location: str | None = None
    bullets: list[Bullet] = Field(min_length=1)


class Project(_Model):
    id: str
    name: str
    dates: str | None = None
    tech: list[str] = Field(default_factory=list)
    link: str | None = None
    demo: str | None = None
    bullets: list[Bullet] = Field(min_length=1)


class MasterCV(_Model):
    name: str
    contact: Contact
    summary: str | None = None
    education: list[Education] = Field(min_length=1)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ids_unique_and_prefixed(self) -> "MasterCV":
        seen: set[str] = set()

        def check(i: str) -> None:
            if i in seen:
                raise ValueError(f"duplicate id {i!r}")
            seen.add(i)

        for e in self.education:
            check(e.id)
        for entry in [*self.experience, *self.projects]:
            check(entry.id)
            for b in entry.bullets:
                if not b.id.startswith(entry.id + "."):
                    raise ValueError(f"bullet id {b.id!r} must start with {entry.id!r}.")
                check(b.id)
        return self


def all_ids(cv: MasterCV) -> set[str]:
    ids = {e.id for e in cv.education}
    for entry in [*cv.experience, *cv.projects]:
        ids.add(entry.id)
        ids.update(b.id for b in entry.bullets)
    return ids


def load_cv(path: str) -> MasterCV:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"{path}: file not found")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
        return MasterCV.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ValueError(f"{path}: {e}") from e


def cv_to_text(cv: MasterCV) -> str:
    """Plain-text CV for the scoring prompt. Location stays (it bears on location requirements);
    email, phone and links are noise."""
    lines = [cv.name]
    if cv.contact.location:
        lines.append(f"Location: {cv.contact.location}")
    if cv.summary:
        lines.append(f"Summary: {cv.summary}")
    lines.append("Education:")
    for e in cv.education:
        extra = f" — {'; '.join(e.details)}" if e.details else ""
        lines.append(f"- {e.degree}, {e.school} ({e.dates}){extra}")
    if cv.experience:
        lines.append("Experience:")
        for x in cv.experience:
            lines.append(f"- {x.title}, {x.company} ({x.dates})")
            lines.extend(f"  • {b.text}" for b in x.bullets)
    if cv.projects:
        lines.append("Projects:")
        for p in cv.projects:
            dates = f" ({p.dates})" if p.dates else ""
            tech = f" [{', '.join(p.tech)}]" if p.tech else ""
            lines.append(f"- {p.name}{dates}{tech}")
            lines.extend(f"  • {b.text}" for b in p.bullets)
    if cv.skills:
        lines.append("Skills:")
        lines.extend(f"- {k}: {', '.join(v)}" for k, v in cv.skills.items())
    return "\n".join(lines)


MAX_EXPERIENCE_ENTRIES = 4
MAX_PROJECT_ENTRIES = 3
# Floors, not targets. They only bind when the model under-selects, and exist because a
# sparse section is a worse resume than an untailored one: two-thirds of a skills list is
# what automated screens match against, and a single-entry section reads as an error.
MIN_ENTRIES = 2
MIN_SKILLS = 30


def cv_to_id_text(cv: MasterCV) -> str:
    """The tailor prompt's view: every selectable item carries the ID the model must quote back.
    Education and the summary are omitted — they are always rendered from the master."""
    lines: list[str] = []
    if cv.experience:
        lines.append("EXPERIENCE:")
        for x in cv.experience:
            where = f", {x.location}" if x.location else ""
            lines.append(f"[{x.id}] {x.title}, {x.company}{where} ({x.dates})")
            lines.extend(f"  [{b.id}] {b.text}" for b in x.bullets)
    if cv.projects:
        lines.append("PROJECTS:")
        for p in cv.projects:
            tech = f" [{', '.join(p.tech)}]" if p.tech else ""
            lines.append(f"[{p.id}] {p.name}{tech}")
            lines.extend(f"  [{b.id}] {b.text}" for b in p.bullets)
    if cv.skills:
        lines.append("SKILLS (group: options):")
        lines.extend(f"  {k}: {', '.join(v)}" for k, v in cv.skills.items())
    return "\n".join(lines)


def _validate_entries(allowed, raw_list, max_bullets, max_entries, kind, warnings):
    """Keep the model's order; drop anything that is not in `allowed`."""
    out, seen = [], set()
    for item in raw_list or []:
        if not isinstance(item, dict):
            warnings.append(f"{kind}: ignored non-object entry {item!r}")
            continue
        entry_id = item.get("id")
        entry = allowed.get(entry_id)
        if entry is None:
            warnings.append(f"{kind}: unknown entry id {entry_id!r} dropped")
            continue
        if entry_id in seen:
            warnings.append(f"{kind}: duplicate entry id {entry_id!r} dropped")
            continue
        seen.add(entry_id)
        own = {b.id for b in entry.bullets}
        bullets, seen_bullets = [], set()
        for bid in item.get("bullets") or []:
            if bid not in own:
                warnings.append(f"{kind}: bullet {bid!r} does not belong to {entry_id!r}; dropped")
                continue
            if bid in seen_bullets:
                continue
            seen_bullets.add(bid)
            bullets.append(bid)
            if len(bullets) == max_bullets:
                break
        if not bullets:   # an entry with no usable bullets is an empty block on the page
            bullets = [b.id for b in entry.bullets][:max_bullets]
            warnings.append(f"{kind}: {entry_id!r} had no usable bullets; used the master's first {len(bullets)}")
        out.append({"id": entry_id, "bullets": bullets})
        if len(out) == max_entries:
            break
    return out


def _top_up_entries(chosen, entries, max_bullets, max_entries):
    """Add master entries the model did not choose, until the section has MIN_ENTRIES.

    It tops up rather than replacing. The model's picks — and its bullet choices within
    them — survive; only the padding comes from the master. Replacing outright meant that a
    candidate with two jobs, whose model correctly picked the one relevant job, got no
    tailoring of that section at all.
    """
    out = list(chosen)
    taken = {item["id"] for item in out}
    for entry in entries:
        if len(out) >= MIN_ENTRIES or len(out) >= max_entries:
            break
        if entry.id in taken:
            continue
        out.append({"id": entry.id, "bullets": [b.id for b in entry.bullets][:max_bullets]})
    return out


def _top_up_skills(chosen: dict, master: dict) -> dict:
    """Pad the skills list to MIN_SKILLS, keeping the model's picks first in each group.

    A four-item skills list on a software CV is worse than no tailoring: it drops the
    keywords automated screens look for, and it reads to a human as a broken generator.
    """
    out = {group: list(items) for group, items in chosen.items() if items}
    total = sum(len(v) for v in out.values())
    for group, options in master.items():
        if total >= MIN_SKILLS:
            break
        kept = out.setdefault(group, [])
        for skill in options:
            if total >= MIN_SKILLS:
                break
            if skill not in kept:
                kept.append(skill)
                total += 1
    return {group: items for group, items in out.items() if items}


def validate_selection(cv: MasterCV, raw: dict, max_bullets: int = 4) -> tuple[dict, list[str]]:
    """Turn the model's reply into a selection that is safe to render.

    Only IDs that exist in THIS CV survive, a bullet must belong to the entry it is listed
    under, skills must be a subset of the master's, and the caps are enforced. The model's
    ordering is respected for everything that survives. Returns (selection, warnings).
    """
    warnings: list[str] = []
    raw = raw or {}

    exp = _validate_entries({e.id: e for e in cv.experience}, raw.get("experience"),
                            max_bullets, MAX_EXPERIENCE_ENTRIES, "experience", warnings)
    proj = _validate_entries({p.id: p for p in cv.projects}, raw.get("projects"),
                             max_bullets, MAX_PROJECT_ENTRIES, "projects", warnings)

    # Per-section top-up: a near-empty section looks thin, but the model's picks are the
    # tailoring, so they stay and the master only fills the gap.
    if len(exp) < MIN_ENTRIES and len(cv.experience) > len(exp):
        exp = _top_up_entries(exp, cv.experience, max_bullets, MAX_EXPERIENCE_ENTRIES)
        warnings.append(f"experience: topped up to {len(exp)} entries from the master")
    if len(proj) < MIN_ENTRIES and len(cv.projects) > len(proj):
        proj = _top_up_entries(proj, cv.projects, max_bullets, MAX_PROJECT_ENTRIES)
        warnings.append(f"projects: topped up to {len(proj)} entries from the master")

    skills: dict[str, list[str]] = {}
    for group, chosen in (raw.get("skills") or {}).items():
        master = cv.skills.get(group)
        if master is None:
            warnings.append(f"skills: unknown group {group!r} dropped")
            continue
        kept = [s for s in (chosen or []) if s in master]
        for s in (chosen or []):
            if s not in master:
                warnings.append(f"skills: {s!r} is not in the master {group!r} list; dropped")
        if kept:
            skills[group] = kept

    selected_count = sum(len(v) for v in skills.values())
    if selected_count < MIN_SKILLS and cv.skills:
        skills = _top_up_skills(skills, cv.skills)
        warnings.append(f"skills: {selected_count} selected; topped up to "
                        f"{sum(len(v) for v in skills.values())} from the master")

    return {"experience": exp, "projects": proj, "skills": skills}, warnings
