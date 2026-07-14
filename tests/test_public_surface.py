import re
from pathlib import Path


def test_public_surface_contains_no_private_nomenclature():
    root = Path(__file__).parents[1]
    forbidden = (
        "G" + "RID",
        "ATELIER" + "_",
        "JUSPAY" + "_",
        "IRO" + "2-",
        "MI" + "MIR",
    )
    failures = []
    for path in root.rglob("*"):
        if not path.is_file() or any(
            part.startswith(".") or part in {"dist", "build", "__pycache__"}
            for part in path.relative_to(root).parts
        ):
            continue
        if path.suffix.lower() not in {".py", ".md", ".toml", ".yaml", ".yml", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").upper()
        for term in forbidden:
            if re.search(rf"(?<![A-Z0-9_]){re.escape(term)}(?![A-Z0-9_])", text):
                failures.append(f"{path.relative_to(root)}: forbidden private term")
    assert not failures, "\n".join(failures)
