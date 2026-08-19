"""Teaching contracts live on the flow, like tools live in the repo toolbox."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


TEACHING_DIRNAME = "references"
EMPTY = "—"


def _is_teaching(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False
    if path.name.startswith("."):
        return False
    if "worker" in path.stem.lower():
        return False
    return True


def list_teaching(root: Path) -> list[Path]:
    folder = Path(root) / TEACHING_DIRNAME
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.glob("*.md")) if _is_teaching(path)]


def build_teaching_plan(
    skill_root: Path,
    harness_dir: Path | None = None,
) -> list[dict[str, str]]:
    skill_root = Path(skill_root).resolve()
    harness = Path(harness_dir).resolve() if harness_dir else None
    skill_files = {path.name: path for path in list_teaching(skill_root)}
    flow_files = {path.name: path for path in list_teaching(harness)} if harness else {}
    names = sorted(set(skill_files) | set(flow_files))
    rows: list[dict[str, str]] = []
    for name in names:
        dest_rel = f"{TEACHING_DIRNAME}/{name}"
        if name in flow_files and name in skill_files:
            if skill_root == harness:
                action = "already_in_flow"
                source = dest_rel
            else:
                action = "already_in_flow"
                source = f"{TEACHING_DIRNAME}/{name}"
        elif name in flow_files:
            action = "already_in_flow"
            source = dest_rel
        else:
            action = "promote"
            source = f"{TEACHING_DIRNAME}/{name}"
        rows.append(
            {
                "id": Path(name).stem,
                "name": name,
                "source": source,
                "destination": dest_rel,
                "action": action,
            }
        )
    return rows


def render_teaching_plan_markdown(plan: list[dict[str, str]]) -> str:
    lines = [
        "## Teaching contracts",
        "",
        "Same rule as tools. Teaching, instruction context, and judge rubrics",
        "live on the **flow** (`flowsteps/flows/<id>/references/`), not in",
        "`~/.codex/skills`. Promote markdown from the skill `references/`.",
        "",
        "| Contract | Existing on the flow | Promote from skill references |",
        "| --- | --- | --- |",
    ]
    if not plan:
        lines.append("| (none) | | |")
        lines.append("")
        return "\n".join(lines)
    for row in plan:
        existing = f"`{row['destination']}`" if row.get("action") == "already_in_flow" else EMPTY
        promote = (
            f"`{row['id']}` ← `{row['source']}`" if row.get("action") == "promote" else EMPTY
        )
        lines.append(f"| `{row['id']}` | {existing} | {promote} |")
    lines.append("")
    return "\n".join(lines)


def copy_teaching_contracts(
    harness: Path,
    audit: dict[str, Any],
    *,
    overwrite: bool = False,
) -> list[str]:
    target = Path(str(audit.get("target") or ""))
    written: list[str] = []
    dest_dir = Path(harness) / TEACHING_DIRNAME
    for row in audit.get("teaching_plan") or []:
        name = str(row.get("name") or Path(str(row.get("destination") or "")).name)
        if not name:
            continue
        candidates = []
        if target:
            candidates.append(target / TEACHING_DIRNAME / name)
        source = row.get("source")
        if source:
            raw = Path(str(source))
            candidates.append(raw if raw.is_absolute() else (target / raw if target else raw))
        src = next((path for path in candidates if path.is_file()), None)
        if src is None:
            continue
        dest = dest_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            if dest.resolve() != src.resolve():
                continue
            continue
        if dest.resolve() == src.resolve():
            continue
        shutil.copy2(src, dest)
        written.append(str(dest))
    return written


def list_flow_teaching_rel(harness: Path) -> list[str]:
    return [f"{TEACHING_DIRNAME}/{path.name}" for path in list_teaching(harness)]
