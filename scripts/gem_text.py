"""A Gem contains the full master prompt, numbered FlowSteps, and named outputs."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any


def read_master_prompt(source: str | Path) -> str:
    """Read the whole prompt without dropping its opening or later sections."""
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8-sig").strip()
    return str(source).strip()


def heading_id(line: str) -> str | None:
    text = str(line or "").strip()
    if not text.startswith("#"):
        return None
    title = text.lstrip("#").strip().strip("`").strip()
    if not title:
        return None
    numbered = re.match(r"FlowStep \d+: .*\(`([a-z][a-z0-9_]*)`\)$", title)
    if numbered:
        return numbered.group(1)
    token = title.split()[0].strip("`").strip()
    return token.lower().replace("-", "_") if token else None


def split_gem_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    chunks: list[str] = []
    for line in str(text or "").splitlines():
        hid = heading_id(line)
        if hid is not None and str(line).strip().startswith("#"):
            if current:
                sections[current] = "\n".join(chunks).strip()
            current = hid
            chunks = []
            continue
        if current:
            chunks.append(line)
    if current:
        sections[current] = "\n".join(chunks).strip()
    return sections


def read_gem_section(source: str | Path, flowstep_id: str) -> str:
    """Body under the heading named after this FlowStep. Empty if missing."""
    fid = str(flowstep_id or "").strip().lower().replace("-", "_")
    if not fid:
        return ""
    path = Path(source) if not isinstance(source, Path) else source
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source[:120] and path.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
    else:
        text = str(source)
    sections = split_gem_sections(text)
    if fid in sections:
        return sections[fid]
    aliases = {
        "rule_of_success": ("rule", "success"),
    }
    for key, extra in aliases.items():
        if fid == key or fid in extra:
            for name in (key, *extra):
                if name in sections:
                    return sections[name]
    return ""


def render_flowstep_gem_sections(
    flowsteps: list[dict[str, Any]] | None,
    *,
    actions: list[dict[str, Any]] | None = None,
    bindings: list[dict[str, Any]] | None = None,
) -> str:
    """Project actual step order and tool bindings; never invent tool names."""
    descriptions = {row["flowstep_id"]: row for row in actions or []}
    refs = {row["tool"]: row["ref"] for row in bindings or []}
    rows: list[str] = []
    for item in flowsteps or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or item.get("tool") or "").strip()
        if not fid:
            continue
        number = len(rows) + 1
        action = descriptions.get(fid, {})
        title = action.get("title") or fid.replace("_", " ").capitalize()
        tool = refs.get(fid) or item.get("tool")
        tool_text = f"`{tool}`" if tool else "None — performed by the milestone handler."
        rows.append(
            f"### FlowStep {number}: {title} (`{fid}`)\n\n"
            f"FlowStep {number} tools: {tool_text}\n\n"
            + (f"{action['summary']}\n" if action.get("summary") else "")
        )
    if not rows:
        return "No FlowSteps declared. Define the internal actions before building this milestone.\n"
    return "\n".join(rows)


OUTLINE_START = "<!-- m8m:execution-plan:start -->"
OUTLINE_END = "<!-- m8m:execution-plan:end -->"


def with_milestone_outline(prompt: str, milestone: dict[str, Any]) -> str:
    """Refresh only the generated outline, keeping all authored prompt text intact."""
    from flowstep_runtime import FlowError, normalize_flowsteps

    flowsteps, _ = normalize_flowsteps(
        flowsteps=milestone.get("flowsteps"), tools=milestone.get("tools"),
    )
    observer = milestone.get("observer") or {}
    steps = render_flowstep_gem_sections(
        flowsteps, actions=observer.get("actions"),
        bindings=(milestone.get("execution") or {}).get("tool_bindings"),
    )
    mid = milestone.get("id") or milestone.get("agent_id")
    title = observer.get("title") or milestone.get("success") or str(mid)
    rows = [OUTLINE_START, f"## Execution plan — {mid} — {title}", "",
            "Read the complete master prompt above, then perform these internal actions.", "",
            steps.rstrip(), "", "## Named outputs", ""]
    for port in milestone.get("outputs") or []:
        rows.append(
            f"- `{port['id']}`: {port.get('name') or port['id']}; {port['kind']}; "
            f"{port.get('cardinality', 'one')}; {'required' if port.get('required') else 'optional'}."
        )
        if milestone.get("output_contract"):
            rows.append(f"  Reference: `{mid}.{port['id']}`; binding: "
                        f"`from: {mid}.{milestone['output_contract']}`, `output: {port['id']}`.")
    if not milestone.get("outputs"):
        rows.append("No named outputs declared. Define the deliverable before building this milestone.")
    rows.append(OUTLINE_END)
    outline = "\n".join(rows)
    start, end = prompt.find(OUTLINE_START), prompt.find(OUTLINE_END)
    if start >= 0 or end >= 0:
        if start < 0 or end < start or prompt.count(OUTLINE_START) != 1 or prompt.count(OUTLINE_END) != 1:
            raise FlowError(f"{mid}: malformed generated execution plan markers")
        return prompt[:start] + outline + prompt[end + len(OUTLINE_END):]
    return prompt + ("\n\n" if not prompt.endswith("\n\n") else "") + outline + "\n"
