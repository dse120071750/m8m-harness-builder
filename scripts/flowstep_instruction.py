"""Generate and update planning/flowstep-instruction.md — the skill instruction."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

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
) -> str:
    statuses = statuses or {}
    last = flow["steps"][-1]
    lines = [
        HEADER,
        f"# FlowStep instruction: {flow['flow_id']}",
        "",
        "This file is the skill instruction. Each section is a milestone.",
        "Use only the listed toolbox functions inside a milestone.",
        "Mark a milestone DONE when its output schema PASSes.",
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
    ]
    if flow.get("_v3"):
        lines.extend(["## Milestones", "", "```mermaid", "flowchart LR"])
        ids = [step["id"] for step in flow["steps"]]
        lines.append("    request[request]")
        if ids:
            lines.append(f"    request --> {ids[0]}")
        for left, right in zip(ids, ids[1:]):
            lines.append(f"    {left} --> {right}")
        lines.extend(["```", ""])
        used = []
        for step in flow["steps"]:
            for tool_id in step.get("tools") or []:
                if tool_id not in used:
                    used.append(tool_id)
        lines.extend(["## Toolbox", ""])
        for tool_id in used:
            lines.append(f"- `{tool_id}` — `flowsteps/tools/{tool_id}/tool.py`")
        lines.extend(["", "## Milestone index", ""])
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_instruction(skill_dir, flow, merged), encoding="utf-8", newline="\n")
    return path


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
