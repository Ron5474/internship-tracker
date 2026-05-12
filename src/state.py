from pathlib import Path


def read_last_sha(data_dir: str) -> str | None:
    path = Path(data_dir) / "last_sha.txt"
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value if value else None


def write_last_sha(data_dir: str, sha: str) -> None:
    path = Path(data_dir) / "last_sha.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha)
