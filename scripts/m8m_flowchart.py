"""Single M8M flowchart markdown: milestones, schema gates, and foreach loops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import normalize_flowsteps, utc_now
from humanize_chart import humanize_flowstep, humanize_milestone
from toolbox_plan import render_toolbox_plan_markdown


FLOWCHART_REL = Path("planning/m8m-flowchart.md")
ELSE_BLOCKED = "BLOCKED"


def flowchart_path(root: Path) -> Path:
    return root / FLOWCHART_REL


def _nodes(items: list[dict[str, Any]], statuses: dict[str, str] | None = None) -> list[dict[str, Any]]:
    statuses = statuses or {}
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
                "status": str(statuses.get(mid) or item.get("status") or ""),
                "branch": item.get("branch") if isinstance(item.get("branch"), dict) else None,
                "on_path": item.get("on_path"),
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
    for item in nodes:
        for path in (item.get("branch") or {}).get("paths") or []:
            if isinstance(path, dict) and path.get("then"):
                then_of.add(str(path["then"]))
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
        elif item.get("branch"):
            paths = []
            for path in (item.get("branch") or {}).get("paths") or []:
                if isinstance(path, dict) and path.get("id"):
                    paths.append(str(path["id"]))
            loop_line = "<br/>branch:" + "/".join(paths[:4])
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
        br = item.get("branch") if isinstance(item.get("branch"), dict) else None
        if br and br.get("paths"):
            for path in br["paths"]:
                if not isinstance(path, dict):
                    continue
                then = str(path.get("then") or "")
                label = str(path.get("id") or "").replace('"', "")
                if then:
                    lines.append(f'    {mid} -->|"{label}"| {then}')
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
            src_path = item.get("on_path")
            dest_path = dest.get("on_path")
            if dest_path and src_path and dest_path != src_path:
                continue
            if dest_path and item.get("branch"):
                continue
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
    statuses: dict[str, str] | None = None,
) -> str:
    nodes = _nodes(items, statuses)
    mermaid = render_mermaid(items)
    lines = [
        f"# M8M flowchart: {title}",
        "",
        "One chart. Milestone to milestone. Each node is a required asset",
        "(file, image, json proof, or data). Missing it is BLOCKED.",
        "FlowSteps inside a node are a guide (one preferred tool each), not a compulsory path.",
        "for = ledger milestone until remaining=0. judge (if) = retry until worker ok.",
        "The JPEG is the audit copy: portable, human-labeled, native to review.",
        "It is rewritten on generate and on every step edit.",
        "",
        f"- flow_id: `{flow_id or title}`",
        f"- source: `{source}`",
        f"- updated_at: {utc_now()}",
        "",
        "## Chart",
        "",
        "Portable JPEG for audit. Humanizer names each milestone and FlowStep.",
        "Regenerated on generate and on every step edit (`write` / `mark`).",
        "",
        f"![M8M flowchart: {title}](m8m-flowchart.jpg)",
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
            "| Milestone | What it means | Asset | Status | Intelligence | Tools | Control |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not nodes:
        lines.append("| (none) | | | | | | |")
    for item in nodes:
        tools = ", ".join(f"`{tool}`" for tool in item["tools"]) or "none"
        if item.get("loop") == "for" and item.get("ledger"):
            fe = item["ledger"]
            control = f"for `{fe.get('path')}` max={fe.get('max_items')}"
        elif item.get("loop") == "judge":
            control = "judge until ok"
        elif item.get("branch"):
            paths = []
            for path in (item.get("branch") or {}).get("paths") or []:
                if isinstance(path, dict) and path.get("id"):
                    paths.append(str(path["id"]))
            control = "branch " + " / ".join(paths)
        elif item.get("on_path"):
            control = f"on_path `{item['on_path']}`"
        else:
            control = "linear"
        asset = item.get("asset_kind") or "required"
        human = humanize_milestone(item)
        status = human.get("status") or "—"
        lines.append(
            f"| `{item['id']}` | {human['title']} | `{asset}` | `{status}` | "
            f"`{item['intelligence']}` | {tools} | {control} |"
        )
    lines.extend(
        [
            "",
            "## FlowSteps (guide)",
            "",
            "Sequence inside each milestone. Prefer the named tool. Optional.",
            "If it fails, recover like a normal agent. The milestone asset is still compulsory.",
            "",
            "| Milestone | # | FlowStep | What it means | Preferred tool |",
            "| --- | ---: | --- | --- | --- |",
        ]
    )
    guide_rows = 0
    for item in nodes:
        for index, fs in enumerate(item.get("flowsteps") or [], start=1):
            guide_rows += 1
            human_fs = humanize_flowstep(fs, index)
            lines.append(
                f"| `{item['id']}` | {index} | `{fs.get('id') or fs.get('tool')}` | "
                f"{human_fs['caption']} | `{fs.get('tool') or '—'}` |"
            )
    if not guide_rows:
        lines.append("| (none) | | | | |")
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
    lines.extend(["", "## Branch (after the milestone)", ""])
    branches = [item for item in nodes if item.get("branch")]
    if not branches:
        lines.append("None. No branch after a milestone.")
    else:
        lines.extend(
            [
                "AI drafts the path. The worker writes `{ok, branch}`. Skip is not BLOCK.",
                "",
                "| Milestone | Worker | Default | Paths | Join |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in branches:
            br = item.get("branch") or {}
            paths = []
            for path in br.get("paths") or []:
                if isinstance(path, dict):
                    paths.append(f"`{path.get('id')}`→`{path.get('then') or path.get('id')}`")
                else:
                    paths.append(f"`{path}`")
            lines.append(
                f"| `{item['id']}` | `{br.get('worker') or item.get('worker') or 'branch_receipt'}` | "
                f"`{br.get('default') or '—'}` | {', '.join(paths)} | `{br.get('join') or '—'}` |"
            )
    lines.extend(
        [
            "",
            "Proceed only when the worker receipt is `ok: true` and the milestone asset PASSes.",
            "Branch is after that PASS. The model drafts; the tool writes `branch`.",
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
    statuses: dict[str, str] | None = None,
    focus_id: str | None = None,
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
            statuses=statuses,
        ),
        encoding="utf-8",
        newline="\n",
    )
    from flowchart_jpg import write_flowchart_jpg

    write_flowchart_jpg(
        path.with_suffix(".jpg"),
        items,
        title=title,
        focus_id=focus_id,
        statuses=statuses,
    )
    return path
