"""Generate and update planning/flowstep-instruction.md — the skill instruction."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from teaching_contracts import list_flow_teaching_rel
from toolbox_plan import render_toolbox_plan_markdown
from tool_vs_intelligence import from_flow as classification_from_flow
from tool_vs_intelligence import render_markdown as render_classification_markdown
from flowstep_runtime import (
    FlowError,
    add_harness_location_args,
    find_flow_path,
    harness_dir_from_args,
    load_flow,
    read_json,
    render_flowstep_table,
    utc_now,
)


INSTRUCTION_REL = Path("planning/flowstep-instruction.md")
HEADER = "<!-- flowstep_instruction_v1 -->"
STATUSES = ("PENDING", "DONE", "BLOCKED")
STATUS_RE = re.compile(r"^- status: (PENDING|DONE|BLOCKED)\s*$", re.M)
HEADING_RE = re.compile(r"^### `([a-z][a-z0-9_]*)`\s*$", re.M)


def instruction_path(skill_dir: Path) -> Path:
    return skill_dir / INSTRUCTION_REL


def expected_return(schema: dict[str, Any]) -> str:
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or properties)
    summary: dict[str, Any] = {}
    for key in required:
        prop = properties.get(key) if isinstance(properties.get(key), dict) else {}
        if "enum" in prop:
            summary[key] = " | ".join(str(item) for item in prop["enum"])
        elif "$ref" in prop:
            summary[key] = prop["$ref"]
        else:
            summary[key] = prop.get("type") or "object"
    return json.dumps(summary, ensure_ascii=False)


def _schema_summary(skill_dir: Path, relative: str) -> str:
    path = skill_dir / relative
    if not path.is_file():
        return "{}"
    try:
        return expected_return(read_json(path))
    except (FlowError, OSError, TypeError):
        return "{}"


def parse_statuses(markdown: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    current: str | None = None
    for line in markdown.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            current = heading.group(1)
            continue
        if current:
            match = STATUS_RE.match(line)
            if match:
                statuses[current] = match.group(1)
                current = None
    return statuses


def render_instruction(
    skill_dir: Path,
    flow: dict[str, Any],
    statuses: dict[str, str] | None = None,
    toolbox_plan: list[dict[str, Any]] | None = None,
) -> str:
    statuses = statuses or {}
    last = flow["steps"][-1]
    lines = [
        HEADER,
        f"# FlowStep instruction: {flow['flow_id']}",
        "",
        "This file is the skill instruction. Each section is a milestone.",
        "A milestone input schema is the previous milestone output schema.",
        "Each milestone is a harness checkpoint: a required asset (file, image, json proof, or data).",
        "Mark DONE when that asset is produced (output schema PASS). Missing it is BLOCKED.",
        "FlowSteps inside a milestone are a guide: prefer one tool each, in table order.",
        "The tool is optional. If it fails, recover like a normal agent. Do not skip the asset.",
        "",
        f"- harness: `{skill_dir}`",
        f"- flow_id: `{flow['flow_id']}`",
        f"- final_payload: `{last['output_contract']}` from `{last['id']}`",
        f"- updated_at: {utc_now()}",
        "",
        "## Run",
        "",
        "```powershell",
        "python <builder>/scripts/run_flow.py --codebase <repo> --flow-id "
        + flow["flow_id"]
        + " --run-dir <run-dir> --request <request.json>",
        "```",
        "",
        "If a milestone returns ACTION_REQUIRED, write only the frozen draft and advance.",
        "",
        "## Tool vs intelligence",
        "",
        "Schema: `tool_vs_intelligence_table_v1`.",
        "",
        render_classification_markdown(classification_from_flow(flow)),
        "",
    ]
    if flow.get("_v3"):
        lines.extend(
            [
                "## Milestones",
                "",
                "The M8M flowchart is `planning/m8m-flowchart.md` plus `planning/m8m-flowchart.jpg`.",
                "The JPEG is rewritten on generate and on every step edit. It is the portable audit copy.",
                "",
            ]
        )
        if toolbox_plan:
            lines.extend(render_toolbox_plan_markdown(toolbox_plan).splitlines())
            lines.append("")
        used = []
        for step in flow["steps"]:
            for tool_id in step.get("tools") or []:
                if tool_id not in used:
                    used.append(tool_id)
        lines.extend(["## Toolbox", ""])
        for tool_id in used:
            lines.append(f"- `{tool_id}` — `flowsteps/tools/{tool_id}/tool.py`")
        teaching = list_flow_teaching_rel(skill_dir)
        lines.extend(
            [
                "",
                "## Teaching contracts",
                "",
                "Same rule as tools. These live on the flow, not in `~/.codex/skills` or `~/.claude/skills`.",
                "",
            ]
        )
        if teaching:
            for rel in teaching:
                lines.append(f"- `{rel}`")
        else:
            lines.append("None. Promote skill `references/*.md` into this flow.")
        lines.append("")
        lines.extend(["## Milestone index", ""])
    else:
        lines.extend(["## Step index", ""])
    table = render_flowstep_table(flow).splitlines()
    # drop the title line from the shared table; keep the markdown table
    body = [line for line in table if not line.startswith("# ")]
    lines.extend(body)
    if lines[-1] != "":
        lines.append("")
    lines.append("## Steps")
    lines.append("")
    for index, step in enumerate(flow["steps"], start=1):
        status = statuses.get(step["id"], "PENDING")
        if status not in STATUSES:
            status = "PENDING"
        inputs = ", ".join(f"{name}={ref}" for name, ref in step["inputs"].items())
        block = [
            f"### `{step['id']}`",
            f"- status: {status}",
            f"- order: {index}",
            f"- class: `{step.get('class', 'tool')}`",
            f"- intelligence: `{step.get('intelligence', step.get('model', 'none'))}`",
            f"- assemble: `{step['handler']}`",
            f"- toolbox: {', '.join(f'`{t}`' for t in (step.get('tools') or [])) or 'none'}",
            f"- flowsteps (guide): "
            + (
                ", ".join(
                    f"`{fs.get('id')}`→`{fs.get('tool') or '—'}`"
                    for fs in (step.get("flowsteps") or [])
                )
                or "none"
            ),
            f"- test: `{step.get('test', f'steps/{step['id']}/tests/test_tool.py')}`",
            f"- model: `{step['model']}`",
            f"- model_justification: {step.get('model_justification') or 'none'}",
            f"- inputs: {inputs}",
            f"- input_schema: `{step['input_schema']}`",
            f"- output_schema: `{step['output_schema']}`",
            f"- output_contract: `{step['output_contract']}`",
            f"- expected_return: `{_schema_summary(skill_dir, step['output_schema'])}`",
        ]
        if step["model"] != "none":
            block.append(
                f"- draft_schema: `{step.get('draft_schema', f'steps/{step['id']}/draft.schema.json')}`"
            )
        block.append("")
        lines.extend(block)
    lines.append("After a step's tool, schemas, and test are real, mark it DONE.")
    lines.append("Do not start the next step while the current step is PENDING.")
    lines.append("")
    return "\n".join(lines)


def write_instruction(
    skill_dir: Path,
    flow: dict[str, Any] | None = None,
    *,
    statuses: dict[str, str] | None = None,
    toolbox_plan: list[dict[str, Any]] | None = None,
    source: str = "edit",
) -> Path:
    skill_dir = skill_dir.resolve()
    flow = flow or load_flow(skill_dir, find_flow_path(skill_dir))
    path = instruction_path(skill_dir)
    merged = {}
    if path.is_file():
        merged.update(parse_statuses(path.read_text(encoding="utf-8")))
    if statuses:
        merged.update(statuses)
    known = {step["id"] for step in flow["steps"]}
    merged = {key: value for key, value in merged.items() if key in known}
    for step in flow["steps"]:
        merged.setdefault(step["id"], "PENDING")
    if toolbox_plan is None:
        saved = skill_dir / "planning" / "toolbox-plan.json"
        if saved.is_file():
            try:
                loaded_plan = json.loads(saved.read_text(encoding="utf-8"))
                if isinstance(loaded_plan, list):
                    toolbox_plan = loaded_plan
            except (OSError, json.JSONDecodeError, TypeError):
                toolbox_plan = None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_instruction(skill_dir, flow, merged, toolbox_plan=toolbox_plan),
        encoding="utf-8",
        newline="\n",
    )
    if flow.get("_v3"):
        refresh_chart(skill_dir, flow, statuses=merged, source=source)
    return path


def refresh_chart(
    skill_dir: Path,
    flow: dict[str, Any],
    statuses: dict[str, str] | None = None,
    source: str = "edit",
) -> Path:
    from m8m_flowchart import write_flowchart
    from toolbox_plan import build_toolbox_plan

    skill_dir = skill_dir.resolve()
    plan = None
    saved = skill_dir / "planning" / "toolbox-plan.json"
    if saved.is_file():
        try:
            loaded_plan = json.loads(saved.read_text(encoding="utf-8"))
            if isinstance(loaded_plan, list):
                plan = loaded_plan
        except (OSError, json.JSONDecodeError, TypeError):
            plan = None
    if plan is None:
        plan = build_toolbox_plan(flow.get("steps") or [])
    return write_flowchart(
        skill_dir,
        flow.get("steps") or [],
        title=str(flow.get("flow_id") or skill_dir.name),
        flow_id=str(flow.get("flow_id") or ""),
        source=source,
        toolbox_plan=plan,
        statuses=statuses,
    )


def mark_step(skill_dir: Path, step_id: str, status: str) -> Path:
    if status not in STATUSES:
        raise FlowError(f"status must be one of {STATUSES}")
    flow = load_flow(skill_dir.resolve(), find_flow_path(skill_dir.resolve()))
    if step_id not in {step["id"] for step in flow["steps"]}:
        raise FlowError(f"unknown step: {step_id}")
    return write_instruction(skill_dir, flow, statuses={step_id: status})


def sync_statuses_from_errors(skill_dir: Path, flow: dict[str, Any], errors: list[str]) -> Path:
    statuses: dict[str, str] = {}
    for step in flow["steps"]:
        step_errors = [item for item in errors if item.startswith(f"{step['id']}:")]
        if not step_errors:
            statuses[step["id"]] = "DONE"
        elif any("generated stub" in item or "{ok: boolean}" in item for item in step_errors):
            statuses[step["id"]] = "PENDING"
        else:
            statuses[step["id"]] = "BLOCKED"
    return write_instruction(skill_dir, flow, statuses=statuses)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("write", "show"):
        item = sub.add_parser(name)
        add_harness_location_args(item)
        item.add_argument("--flow")
    mark = sub.add_parser("mark")
    add_harness_location_args(mark)
    mark.add_argument("--step", required=True)
    mark.add_argument("--status", required=True, choices=STATUSES)
    mark.add_argument("--flow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skill_dir = harness_dir_from_args(args)
    try:
        flow = load_flow(skill_dir, find_flow_path(skill_dir, getattr(args, "flow", None)))
        if args.command == "mark":
            path = mark_step(skill_dir, args.step, args.status)
        else:
            path = write_instruction(skill_dir, flow)
        text = path.read_text(encoding="utf-8")
        if args.command == "show":
            print(text, end="")
            return 0
        print(
            json.dumps(
                {
                    "schema": "flowstep_instruction_result_v1",
                    "status": "PASS",
                    "instruction_path": str(path),
                    "statuses": parse_statuses(text),
                },
                indent=2,
            )
        )
        return 0
    except FlowError as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
