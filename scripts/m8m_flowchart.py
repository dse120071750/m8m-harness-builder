"""Single M8M flowchart markdown: milestones, schema gates, and foreach loops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import normalize_flowsteps, utc_now
from toolbox_plan import render_toolbox_plan_markdown


FLOWCHART_REL = Path("planning/m8m-flowchart.md")
ELSE_BLOCKED = "BLOCKED"


def flowchart_path(root: Path) -> Path:
    return root / FLOWCHART_REL


def _nodes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in items:
        mid = str(item.get("id") or "")
        if not mid:
            continue
        ledger = item.get("ledger") if isinstance(item.get("ledger"), dict) else None
        if ledger is None and isinstance(item.get("foreach"), dict):
            ledger = item["foreach"]
        nodes.append(
            {
                "id": mid,
                "intelligence": item.get("intelligence") or item.get("model") or "none",
                "tools": [str(tool) for tool in (item.get("tools") or []) if tool],
                "loop": str(item.get("loop") or "none"),
                "ledger": ledger,
                "worker": item.get("worker"),
                "receipt_schema": item.get("receipt_schema"),
                "next": [],
                "else": None,
                "join": None,
                "output_contract": str(item.get("output_contract") or ""),
                "asset_kind": str(((item.get("asset") or {}).get("kind") if isinstance(item.get("asset"), dict) else "") or ""),
                "flowsteps": normalize_flowsteps(
                    flowsteps=item.get("flowsteps"),
                    tools=item.get("tools"),
                )[0],
            }
        )
    return nodes


def _when_label(edge: dict[str, Any]) -> str:
    schema = edge.get("schema")
    if isinstance(schema, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = list(schema.get("required") or [])
        for field in required + [key for key in props if key not in required]:
            prop = props.get(field)
            if not isinstance(prop, dict):
                continue
            if "const" in prop:
                return f"{field}={prop['const']}"
            if "enum" in prop:
                return str(field)
    when = str(edge.get("when") or "gate")
    stem = Path(when).stem
    if stem.endswith(".schema"):
        stem = stem[: -len(".schema")]
    if "_" in stem:
        field, value = stem.rsplit("_", 1)
        if field and value:
            return f"{field}={value}"
    return stem


def _blocked_id(mid: str) -> str:
    return f"blocked_{mid}"


def render_mermaid(items: list[dict[str, Any]]) -> str:
    nodes = _nodes(items)
    ids = [item["id"] for item in nodes]
    then_of = {str(edge["then"]) for item in nodes for edge in item["next"]}
    join_ids = {item["id"] for item in nodes if item.get("join")}
    lines = ["flowchart TD", "    request([request])"]
    for item in nodes:
        mid = item["id"]
        tools = ",".join(item["tools"] or [])
        tool_line = f"<br/>{tools}" if tools else ""
        asset_kind = item.get("asset_kind") or ""
        asset_line = f"<br/>asset:{asset_kind}" if asset_kind else ""
        loop_line = ""
        if item.get("loop") == "for" and item.get("ledger"):
            fe = item["ledger"]
            loop_line = f"<br/>for:{fe.get('path')} max={fe.get('max_items')}"
        elif item.get("loop") == "judge":
            loop_line = "<br/>judge until ok"
        extra = ""
        if item["intelligence"] not in {None, "none"}:
            extra = f"<br/>intel:{item['intelligence']}"
        lines.append(f'    {mid}["{mid}{extra}{asset_line}{loop_line}{tool_line}"]')
    if ids:
        lines.append(f"    request --> {ids[0]}")
    for item in nodes:
        mid = item["id"]
        if item["next"]:
            for edge in item["next"]:
                label = _when_label(edge).replace('"', "")
                lines.append(f'    {mid} -->|"{label}"| {edge["then"]}')
            else_to = item.get("else") or ELSE_BLOCKED
            if else_to in {ELSE_BLOCKED, "BLOCKED"}:
                lines.append(f'    {mid} -->|"else BLOCKED"| {_blocked_id(mid)}')
            else:
                lines.append(f'    {mid} -->|"else"| {else_to}')
            continue
    by_id = {item["id"]: item for item in nodes}
    for item in nodes:
        if item.get("join"):
            for src in item["join"]:
                if src in by_id:
                    lines.append(f'    {src} -->|"join"| {item["id"]}')
    for index, item in enumerate(nodes):
        if item["next"]:
            continue
        if item.get("join"):
            pass
        for later in ids[index + 1 :]:
            if later in then_of:
                continue
            dest = by_id[later]
            if dest.get("join") and item["id"] in (dest.get("join") or []):
                break
            if dest.get("join"):
                continue
            lines.append(f"    {item['id']} --> {later}")
            break
    return "\n".join(lines)


def render_flowchart(
    items: list[dict[str, Any]],
    *,
    title: str,
    flow_id: str | None = None,
    source: str = "audit",
    toolbox_plan: list[dict[str, Any]] | None = None,
) -> str:
    nodes = _nodes(items)
    mermaid = render_mermaid(items)
    lines = [
        f"# M8M flowchart: {title}",
        "",
        "One chart. Milestone to milestone. Each node is a required asset",
        "(file, image, json proof, or data). Missing it is BLOCKED.",
        "FlowSteps inside a node are a guide (one preferred tool each), not a compulsory path.",
        "for = ledger milestone until remaining=0. judge (if) = retry until worker ok.",
        "GitHub publishes a JPEG, not a mermaid rich display.",
        "",
        f"- flow_id: `{flow_id or title}`",
        f"- source: `{source}`",
        f"- updated_at: {utc_now()}",
        "",
        "## Chart",
        "",
        "GitHub does not render this as a diagram. Publish a JPEG next to this file.",
        "",
        "```text",
        mermaid,
        "```",
        "",
    ]
    if toolbox_plan:
        lines.extend(render_toolbox_plan_markdown(toolbox_plan).splitlines())
        lines.append("")
    lines.extend(
        [
            "## Nodes",
            "",
            "| Milestone | Asset | Intelligence | Tools | Control |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if not nodes:
        lines.append("| (none) | | | | |")
    for item in nodes:
        tools = ", ".join(f"`{tool}`" for tool in item["tools"]) or "none"
        if item.get("loop") == "for" and item.get("ledger"):
            fe = item["ledger"]
            control = f"for `{fe.get('path')}` max={fe.get('max_items')}"
        elif item.get("loop") == "judge":
            control = "judge until ok"
        else:
            control = "linear"
        asset = item.get("asset_kind") or "required"
        lines.append(
            f"| `{item['id']}` | `{asset}` | `{item['intelligence']}` | {tools} | {control} |"
        )
    lines.extend(
        [
            "",
            "## FlowSteps (guide)",
            "",
            "Sequence inside each milestone. Prefer the named tool. Optional.",
            "If it fails, recover like a normal agent. The milestone asset is still compulsory.",
            "",
            "| Milestone | # | FlowStep | Preferred tool |",
            "| --- | ---: | --- | --- |",
        ]
    )
    guide_rows = 0
    for item in nodes:
        for index, fs in enumerate(item.get("flowsteps") or [], start=1):
            guide_rows += 1
            lines.append(
                f"| `{item['id']}` | {index} | `{fs.get('id') or fs.get('tool')}` | "
                f"`{fs.get('tool') or '—'}` |"
            )
    if not guide_rows:
        lines.append("| (none) | | | |")
    lines.extend(["", "## For (ledger)", ""])
    fors = [item for item in nodes if item.get("loop") == "for"]
    if not fors:
        lines.append("None. No ledger-walking milestone.")
    else:
        lines.extend(
            [
                "| Milestone | Ledger path | Item schema | max_items | Worker |",
                "| --- | --- | --- | ---: | --- |",
            ]
        )
        for item in fors:
            fe = item.get("ledger") or {}
            lines.append(
                f"| `{item['id']}` | `{fe.get('path')}` | `{fe.get('item_schema')}` | "
                f"{fe.get('max_items')} | `{item.get('worker') or 'ledger_receipt'}` |"
            )
    lines.extend(["", "## Judge (until ok)", ""])
    judges = [item for item in nodes if item.get("loop") == "judge"]
    if not judges:
        lines.append("None. No judge-until-ok milestone.")
    else:
        lines.extend(
            [
                "| Milestone | Worker | Receipt schema |",
                "| --- | --- | --- |",
            ]
        )
        for item in judges:
            lines.append(
                f"| `{item['id']}` | `{item.get('worker') or 'ok_receipt'}` | "
                f"`{item.get('receipt_schema') or 'schemas/ok_receipt.json'}` |"
            )
    lines.extend(
        [
            "",
            "Proceed only when the worker receipt is `ok: true` and the milestone asset PASSes.",
            "",
        ]
    )
    return "\n".join(lines)


def write_flowchart(
    root: Path,
    items: list[dict[str, Any]],
    *,
    title: str,
    flow_id: str | None = None,
    source: str = "audit",
    toolbox_plan: list[dict[str, Any]] | None = None,
) -> Path:
    path = flowchart_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_flowchart(
            items,
            title=title,
            flow_id=flow_id,
            source=source,
            toolbox_plan=toolbox_plan,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path
