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
