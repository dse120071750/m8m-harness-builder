"""Per-milestone toolbox origin: existing, promote from a skill script, or generate new."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_tools import infer_codebase, tools_root


BUILDER_ROOT = Path(__file__).resolve().parents[1]
SEEDS_DIR = BUILDER_ROOT / "seeds"
EMPTY = "—"


def seed_ids() -> set[str]:
    if not SEEDS_DIR.is_dir():
        return {"hash_bind", "schema_validate"}
    return {path.name for path in SEEDS_DIR.iterdir() if path.is_dir() and (path / "tool.py").is_file()}


def existing_toolbox_ids(codebase: Path | None) -> set[str]:
    found = set(seed_ids())
    if codebase is None:
        return found
    root = tools_root(Path(codebase))
    if not root.is_dir():
        return found
    for path in root.iterdir():
        if path.is_dir() and (path / "tool.py").is_file():
            found.add(path.name)
    return found


def _promotion_index(python_standardization: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in python_standardization or []:
        tool_id = str(row.get("tool_id") or "")
        if tool_id:
            index[tool_id] = row
    return index


def _is_skill_script(row: dict[str, Any]) -> bool:
    if row.get("action") == "already_python":
        return False
    source = str(row.get("source") or "")
    if not source:
        return False
    if source.startswith("suggested:") or source.startswith("flow:"):
        return False
    if source.startswith("SKILL.md"):
        return False
    return True


def _intel_cell(item: dict[str, Any]) -> str:
    intel = str(item.get("intelligence") or item.get("model") or "none")
    note = str(item.get("model_justification") or "").strip()
    if intel in {"", "none"}:
        return "`none`"
    if note:
        return f"`{intel}` ({note})"
    return f"`{intel}`"


def _join_cell(parts: list[str]) -> str:
    return "<br>".join(parts) if parts else EMPTY


def classify_tool(
    tool_id: str,
    *,
    existing: set[str],
    promotions: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    row = promotions.get(tool_id)
    if row and _is_skill_script(row):
        return "promote", row
    if tool_id in existing:
        return "existing", row
    if row and row.get("action") == "already_python":
        return "existing", row
    return "generate", row


def build_toolbox_plan(
    milestones: list[dict[str, Any]],
    python_standardization: list[dict[str, Any]] | None = None,
    *,
    codebase: Path | None = None,
    existing_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    existing = existing_ids if existing_ids is not None else existing_toolbox_ids(codebase)
    promotions = _promotion_index(python_standardization or [])
    rows: list[dict[str, Any]] = []
    for item in milestones:
        mid = str(item.get("id") or "")
        if not mid:
            continue
        tools = [str(tool) for tool in (item.get("tools") or []) if tool]
        fe = item.get("foreach") if isinstance(item.get("foreach"), dict) else None
        if fe:
            for tool in fe.get("tools") or []:
                if tool and str(tool) not in tools:
                    tools.append(str(tool))
        existing_cell: list[str] = []
        promote_cell: list[dict[str, str]] = []
        generate_cell: list[str] = []
        for tool_id in tools:
            origin, row = classify_tool(tool_id, existing=existing, promotions=promotions)
            if origin == "promote" and row is not None:
                promote_cell.append(
                    {
                        "tool_id": tool_id,
                        "source": str(row.get("source") or row.get("current") or ""),
                    }
                )
            elif origin == "existing":
                existing_cell.append(tool_id)
            else:
                generate_cell.append(tool_id)
        rows.append(
            {
                "milestone": mid,
                "intelligence": item.get("intelligence") or item.get("model") or "none",
                "intelligence_note": str(item.get("model_justification") or "").strip(),
                "existing": existing_cell,
                "promote": promote_cell,
                "generate": generate_cell,
                "tools": tools,
            }
        )
    return rows


def render_toolbox_plan_markdown(plan: list[dict[str, Any]]) -> str:
    lines = [
        "## Toolbox plan",
        "",
        "Tools on each proposed milestone. **Existing toolbox** = already in",
        "`<repo>/flowsteps/tools/` or an M8M seed. **Promote from a skill script** =",
        "skill-private Python becomes that tool. **Generate new** = builder should",
        "develop this tool; a stub is a successful sketch.",
        "",
        "| Milestone | Intelligence | Existing toolbox | Promote from a skill script | Generate new |",
        "| --- | --- | --- | --- | --- |",
    ]
    if not plan:
        lines.append("| (none) | | | | |")
        lines.append("")
        return "\n".join(lines)
    for row in plan:
        existing = _join_cell([f"`{tool}`" for tool in row.get("existing") or []])
        promote = _join_cell(
            [
                f"`{item['tool_id']}` ← `{item['source']}`"
                for item in row.get("promote") or []
            ]
        )
        generate = _join_cell([f"`{tool}`" for tool in row.get("generate") or []])
        intel = _intel_cell(
            {
                "intelligence": row.get("intelligence"),
                "model_justification": row.get("intelligence_note"),
            }
        )
        lines.append(
            f"| `{row['milestone']}` | {intel} | {existing} | {promote} | {generate} |"
        )
    lines.append("")
    return "\n".join(lines)
