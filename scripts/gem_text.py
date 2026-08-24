"""Milestone gem: rule of success for the judge, plus one prompt section per FlowStep."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def heading_id(line: str) -> str | None:
    text = str(line or "").strip()
    if not text.startswith("#"):
        return None
    title = text.lstrip("#").strip().strip("`").strip()
    if not title:
        return None
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


def render_flowstep_gem_sections(flowsteps: list[dict[str, Any]] | None) -> str:
    rows: list[str] = []
    for item in flowsteps or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or item.get("tool") or "").strip()
        if not fid:
            continue
        tool = str(item.get("tool") or "—").strip() or "—"
        rows.append(
            f"## `{fid}`\n\n"
            f"Preferred tool: `{tool}`.\n\n"
            "This section is the prompt for this FlowStep. Write the contract here. "
            "The session reads this when doing this step. It is not a canvas node. "
            "The model may not set `ok`.\n"
        )
    if not rows:
        return (
            "No extra FlowStep prompts. Prefer the listed tools in table order.\n"
        )
    return "\n".join(rows)