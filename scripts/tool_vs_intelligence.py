"""Fixed tool-vs-intelligence table: schema, doctrine rows, and markdown."""

from __future__ import annotations

from typing import Any

TABLE_SCHEMA = "tool_vs_intelligence_table_v1"
TOOL_TEST = "same input → same action; fixture-testable; receipt not opinion; junior can implement from schema"
INTEL_TEST = "fails at least one of the four tests; no fixture without a model"

DOCTRINE_ROWS: list[dict[str, str]] = [
    {
        "id": "fetch_record",
        "class": "tool",
        "test": "same id → same record",
        "why": "structured read; MCP/DB/HTTP GET",
        "destination": "flowsteps/tools/fetch_record/",
    },
    {
        "id": "crop_4x5",
        "class": "tool",
        "test": "fixture PNG in, PNG+hash out",
        "why": "pixel math; not a milestone",
        "destination": "flowsteps/tools/crop_4x5/",
    },
    {
        "id": "hash_bind",
        "class": "tool",
        "test": "same bytes → same sha256",
        "why": "pure bind; file_ref_v2 receipt",
        "destination": "flowsteps/tools/hash_bind/",
    },
    {
        "id": "schema_validate",
        "class": "tool",
        "test": "pass/fail from rules",
        "why": "JSON Schema gate",
        "destination": "flowsteps/tools/schema_validate/",
    },
    {
        "id": "render_html_shell",
        "class": "tool",
        "test": "fixture HTML → screenshot hash",
        "why": "fixed viewport generator",
        "destination": "flowsteps/tools/render_html_shell/",
    },
    {
        "id": "materialize_package",
        "class": "tool",
        "test": "typed inputs → zip/manifest",
        "why": "assembly of already-valid receipts",
        "destination": "flowsteps/tools/materialize_package/",
    },
    {
        "id": "plan_frozen",
        "class": "intelligence",
        "test": "no fixture without a model",
        "why": "editorial plan is not a typed transform",
        "milestone": "plan_frozen",
    },
    {
        "id": "choose_lesson",
        "class": "intelligence",
        "test": "no fixture without a model",
        "why": "judgment among plausible alternatives",
        "milestone": "plan_frozen",
    },
    {
        "id": "image_generate",
        "class": "intelligence",
        "test": "model produces bytes",
        "why": "invention; hash_bind still sizes and binds",
        "milestone": "assets_bound",
    },
    {
        "id": "release_judge",
        "class": "intelligence",
        "test": "not a pixel measurement",
        "why": "taste / teaching quality; footer geometry stays a tool",
        "milestone": "release_packaged",
    },
]


def make_table(rows: list[dict[str, Any]], *, flow_id: str | None = None) -> dict[str, Any]:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item_id = str(row.get("id") or "").strip()
        klass = row.get("class")
        if not item_id or klass not in {"tool", "intelligence"} or item_id in seen:
            continue
        seen.add(item_id)
        entry: dict[str, Any] = {
            "id": item_id,
            "class": klass,
            "test": str(row.get("test") or (TOOL_TEST if klass == "tool" else INTEL_TEST)),
            "why": str(row.get("why") or ("premade toolbox function" if klass == "tool" else "judgment the schema cannot compute")),
        }
        if row.get("milestone"):
            entry["milestone"] = str(row["milestone"])
        if row.get("destination"):
            entry["destination"] = str(row["destination"])
        elif klass == "tool":
            entry["destination"] = f"flowsteps/tools/{item_id}/"
        cleaned.append(entry)
    table: dict[str, Any] = {"schema": TABLE_SCHEMA, "rows": cleaned}
    if flow_id:
        table["flow_id"] = flow_id
    return table


def doctrine_table() -> dict[str, Any]:
    return make_table(DOCTRINE_ROWS, flow_id="f8f_doctrine")


def from_audit(audit: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in audit.get("proposed_milestones") or []:
        mid = str(item.get("id") or "")
        intel = item.get("intelligence") or "none"
        if intel not in {None, "none"}:
            rows.append(
                {
                    "id": mid,
                    "class": "intelligence",
                    "test": INTEL_TEST,
                    "why": item.get("model_justification") or f"{intel} on milestone {mid}",
                    "milestone": mid,
                }
            )
        for tool_id in item.get("tools") or []:
            rows.append(
                {
                    "id": str(tool_id),
                    "class": "tool",
                    "test": TOOL_TEST,
                    "why": "listed toolbox function for this milestone",
                    "milestone": mid,
                    "destination": f"flowsteps/tools/{tool_id}/",
                }
            )
    for item in audit.get("python_standardization") or []:
        tool_id = str(item.get("tool_id") or "")
        if tool_id:
            rows.append(
                {
                    "id": tool_id,
                    "class": "tool",
                    "test": TOOL_TEST,
                    "why": item.get("reason") or "promote to project toolbox",
                    "destination": item.get("destination") or f"flowsteps/tools/{tool_id}/",
                }
            )
    flow_id = (audit.get("grade") or {}).get("flow_id") or (audit.get("audited_skill") or {}).get("name")
    table = make_table(rows, flow_id=str(flow_id) if flow_id else None)
    if not table["rows"]:
        table = make_table(
            [
                {
                    "id": "hash_bind",
                    "class": "tool",
                    "test": TOOL_TEST,
                    "why": "default seed; last milestone emits asset",
                    "destination": "flowsteps/tools/hash_bind/",
                }
            ],
            flow_id=str(flow_id) if flow_id else None,
        )
    return table


def from_flow(flow: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for step in flow.get("steps") or []:
        mid = str(step.get("id") or "")
        intel = step.get("intelligence") or step.get("model") or "none"
        if intel not in {None, "none"}:
            rows.append(
                {
                    "id": mid,
                    "class": "intelligence",
                    "test": INTEL_TEST,
                    "why": step.get("model_justification") or f"{intel} on milestone {mid}",
                    "milestone": mid,
                }
            )
        for tool_id in step.get("tools") or []:
            rows.append(
                {
                    "id": str(tool_id),
                    "class": "tool",
                    "test": TOOL_TEST,
                    "why": "listed toolbox function for this milestone",
                    "milestone": mid,
                    "destination": f"flowsteps/tools/{tool_id}/",
                }
            )
        if not step.get("tools") and (intel in {None, "none"}):
            klass = "tool" if step.get("class") != "intelligence" else "intelligence"
            rows.append(
                {
                    "id": mid,
                    "class": klass,
                    "test": TOOL_TEST if klass == "tool" else INTEL_TEST,
                    "why": step.get("handler") or mid,
                    "milestone": mid,
                }
            )
    return make_table(rows, flow_id=str(flow.get("flow_id") or "") or None)


def render_markdown(table: dict[str, Any]) -> str:
    lines = [
        "| id | class | test | why |",
        "| --- | --- | --- | --- |",
    ]
    for row in table.get("rows") or []:
        lines.append(
            f"| `{row['id']}` | `{row['class']}` | {row['test']} | {row['why']} |"
        )
    if len(lines) == 2:
        lines.append("| (none) | | | |")
    return "\n".join(lines)
