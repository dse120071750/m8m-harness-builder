"""Single M8M flowchart markdown: milestones, schema gates, and foreach loops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import utc_now


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
        foreach = item.get("foreach") if isinstance(item.get("foreach"), dict) else None
        join = item.get("join")
        nodes.append(
            {
                "id": mid,
                "intelligence": item.get("intelligence") or item.get("model") or "none",
                "tools": [str(tool) for tool in (item.get("tools") or []) if tool],
                "next": [
                    edge
                    for edge in (item.get("next") or [])
                    if isinstance(edge, dict) and edge.get("then")
                ],
                "else": item.get("else"),
                "foreach": foreach,
                "join": [str(src) for src in join] if isinstance(join, list) else None,
                "output_contract": str(item.get("output_contract") or ""),
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
        fe = item.get("foreach")
        if fe:
            tools = ",".join(fe.get("tools") or item["tools"] or ["tools"])
            lines.append(
                f'    {mid}[["foreach {fe.get("path")} max={fe.get("max_items")}<br/>{mid}<br/>{tools}"]]'
            )
        elif item["next"]:
            lines.append(f"    {mid}{{{mid}}}")
        else:
            extra = ""
            if item["intelligence"] not in {None, "none"}:
                extra = f"<br/>intel:{item['intelligence']}"
            lines.append(f'    {mid}["{mid}{extra}"]')
        if item["next"] and (item.get("else") or ELSE_BLOCKED) in {None, ELSE_BLOCKED, "BLOCKED"}:
            lines.append(f"    {_blocked_id(mid)}{{{{{ELSE_BLOCKED}}}}}")
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
        fe = item.get("foreach")
        if fe:
            path = fe.get("path") or "item"
            tools = ",".join(fe.get("tools") or item["tools"] or [])
            lines.append(f'    {mid} -->|"each {path} {tools}"| {mid}')
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
            label = None
            if item.get("foreach"):
                label = f'collect {item["foreach"].get("path")}'
            if label:
                lines.append(f'    {item["id"]} -->|"{label}"| {later}')
            else:
                lines.append(f"    {item['id']} --> {later}")
            break
    return "\n".join(lines)


def render_flowchart(
    items: list[dict[str, Any]],
    *,
    title: str,
    flow_id: str | None = None,
    source: str = "audit",
) -> str:
    nodes = _nodes(items)
    mermaid = render_mermaid(items)
    lines = [
        f"# M8M flowchart: {title}",
        "",
        "One chart. Milestone to milestone. If/else and foreach are JSON Schema,",
        "not semantic approval. Intelligence does not pick `then`.",
        "",
        f"- flow_id: `{flow_id or title}`",
        f"- source: `{source}`",
        f"- updated_at: {utc_now()}",
        "",
        "## Chart",
        "",
        "```mermaid",
        mermaid,
        "```",
        "",
        "## Nodes",
        "",
        "| Milestone | Intelligence | Tools | Control |",
        "| --- | --- | --- | --- |",
    ]
    if not nodes:
        lines.append("| (none) | | | |")
    for item in nodes:
        tools = ", ".join(f"`{tool}`" for tool in item["tools"]) or "none"
        if item["next"]:
            control = "gate"
        elif item.get("join"):
            control = "join"
        elif item.get("foreach"):
            fe = item["foreach"]
            control = f"foreach `{fe.get('path')}` max={fe.get('max_items')}"
        else:
            control = "linear"
        lines.append(
            f"| `{item['id']}` | `{item['intelligence']}` | {tools} | {control} |"
        )
    lines.extend(["", "## Gates (if / else)", ""])
    gate_rows = [item for item in nodes if item["next"]]
    if not gate_rows:
        lines.append("None. Linear chain; no `next.when`.")
    else:
        lines.extend(
            [
                "| From | When (JSON Schema) | Then |",
                "| --- | --- | --- |",
            ]
        )
        for item in gate_rows:
            for edge in item["next"]:
                lines.append(
                    f"| `{item['id']}` | `{edge.get('when')}` `{_when_label(edge)}` | `{edge['then']}` |"
                )
            lines.append(
                f"| `{item['id']}` | else | `{item.get('else') or ELSE_BLOCKED}` |"
            )
    lines.extend(["", "## Loops (foreach)", ""])
    loops = [item for item in nodes if item.get("foreach")]
    if not loops:
        lines.append("None. No typed array with `maxItems` on a tool milestone.")
    else:
        lines.extend(
            [
                "| Milestone | Path | Item schema | Tools | max_items | Collect |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for item in loops:
            fe = item["foreach"]
            tools = ", ".join(f"`{tool}`" for tool in (fe.get("tools") or item["tools"])) or "none"
            lines.append(
                f"| `{item['id']}` | `{fe.get('path')}` | `{fe.get('item_schema')}` | {tools} | "
                f"{fe.get('max_items')} | `{fe.get('collect') or fe.get('path')}` |"
            )
    lines.extend(
        [
            "",
            "Criterion is `schema_validate` (Draft 2020-12). There is no loop-until-the-model-is-happy.",
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
) -> Path:
    path = flowchart_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_flowchart(items, title=title, flow_id=flow_id, source=source),
        encoding="utf-8",
        newline="\n",
    )
    return path
