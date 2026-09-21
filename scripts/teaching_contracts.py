"""Teaching contracts live on the flow, like tools live in the repo toolbox."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from gem_text import with_milestone_outline
from milestone_pair import is_wait_milestone


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
        "`~/.codex/skills` or `~/.claude/skills`. Promote markdown from the",
        "skill `references/`.",
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


def write_milestone_gems(
    harness: Path,
    milestones: list[dict[str, Any]],
    *,
    overwrite: bool = False,
) -> list[str]:
    """Preserve each master prompt and project its current steps, tools, and outputs."""
    from flowstep_runtime import skill_rel
    from humanize_chart import success_line, title_id

    written: list[str] = []
    template = Path(__file__).resolve().parents[1] / "templates" / "milestone" / "gem.md"
    body = template.read_text(encoding="utf-8") if template.is_file() else (
        "# __MID__ — __TITLE__\n\n## Master prompt\n\n__SUCCESS__\n"
    )
    for item in milestones:
        mid = str(item.get("id") or item.get("agent_id") or "").strip()
        if not mid:
            continue
        dest = skill_rel(Path(harness), str(item.get("gem") or f"references/{mid}.md"))
        existing = dest.read_text(encoding="utf-8") if dest.is_file() else None
        authored_prompt = item.get("master_prompt")
        if existing is not None and not overwrite:
            authored_prompt = existing
        if isinstance(authored_prompt, str) and authored_prompt.strip():
            text = with_milestone_outline(authored_prompt, item)
            if text == existing:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8", newline="\n")
            written.append(str(dest))
            continue
        declared_outputs = item.get("outputs") if isinstance(item.get("outputs"), list) else []
        kind = str(
            ((declared_outputs[0] or {}).get("kind") if declared_outputs and isinstance(declared_outputs[0], dict) else "")
            or "required"
        )
        loop = str(item.get("loop") or "none")
        worker = str(item.get("worker") or "")
        if loop == "judge" and is_wait_milestone(item):
            judge_line = (
                "- Loop: judge (wait). No draft yet → pause the roster "
                "(row waiting, session exits). Resume: find <run>/roster.json, "
                "write milestones/<id>/work/draft.json. FlowSteps follow the Gem; "
                "the judge evaluates the derived expectation and current candidate. "
                "RETRY → keep working on this box. PASS → next."
            )
        elif loop == "judge":
            judge_line = "- Loop: judge. The separate worker evaluates the derived expectation and returns PASS, RETRY, or BLOCKED."
        elif worker:
            judge_line = "- Loop: none. The worker accepts the current candidate in one shot."
        else:
            judge_line = "- Loop: none. Structural admission validates the closed candidate; no semantic judge runs."
        intelligence = str(item.get("intelligence") or "none")
        if intelligence == "none":
            classification = (
                "This milestone is deterministic tool work (`model: none`). Each "
                "FlowStep uses an existing capability through its declared binding."
            )
        else:
            justification = str(
                item.get("model_justification")
                or "the candidate requires judgment that a fixture cannot decide"
            ).strip()
            classification = (
                f"This milestone uses bounded `{intelligence}` intelligence because "
                f"{justification}. Deterministic sub-operations remain declared "
                "tools; the model returns only candidate data."
            )
        input_lines = []
        for name, binding in (item.get("inputs") or {}).items():
            if isinstance(binding, dict):
                fields = "; ".join(
                    f"`{key}: {binding[key]}`" for key in ("from", "output", "member") if key in binding
                )
                input_lines.append(f"- `{name}`: {fields}.")
            else:
                input_lines.append(f"- `{name}`: `{binding}`.")
        text = (
            body.replace("__TITLE__", str((item.get("observer") or {}).get("title") or item.get("success") or title_id(mid)))
            .replace("__SUCCESS__", str(item.get("success") or success_line(item)))
            .replace("__MID__", mid)
            .replace("__KIND__", kind)
            .replace("__WORKER__", worker or "—")
            .replace("__JUDGE_LINE__", judge_line)
            .replace("__CLASSIFICATION__", classification)
            .replace("__INPUTS__", "\n".join(input_lines) or "Use the request and inputs bound to this milestone.")
        )
        text = with_milestone_outline(text, item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
        written.append(str(dest))
    return written
