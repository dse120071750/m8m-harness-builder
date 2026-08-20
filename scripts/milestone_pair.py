"""One gem + one worker per milestone. Judge loop only when exist ≠ good."""

from __future__ import annotations

from typing import Any

GENERIC_JUDGE = {"ok_receipt", ""}
GATE_TOKENS = ("gate", "judge", "evaluate", "alignment")


def gem_path(milestone_id: str) -> str:
    return f"references/{milestone_id}.md"


def judge_worker_id(milestone_id: str) -> str:
    return f"{milestone_id}_judge"


def asset_kind(item: dict[str, Any]) -> str:
    asset = item.get("asset") if isinstance(item.get("asset"), dict) else {}
    return str(
        item.get("asset_kind")
        or asset.get("kind")
        or ""
    ).strip().lower()


def pick_gate_tool(tools: Any) -> str | None:
    for raw in tools or []:
        tool_id = str(raw or "").strip()
        if not tool_id or tool_id in GENERIC_JUDGE:
            continue
        token = tool_id.lower().replace("-", "_")
        parts = set(token.split("_"))
        if parts & set(GATE_TOKENS) or any(g in token for g in GATE_TOKENS):
            return tool_id
    return None


def exist_worker(kind: str) -> str:
    """File/image: hash_bind. JSON/data: the closed output schema is the exist check."""
    if kind in {"file", "image"}:
        return "hash_bind"
    return ""


def needs_judge(item: dict[str, Any]) -> bool:
    if str(item.get("loop") or "none") == "judge":
        return True
    intel = str(item.get("intelligence") or "none")
    if intel in {"image", "judge"}:
        return True
    name = str(item.get("id") or "").lower().replace("-", "_")
    parts = set(name.split("_"))
    return bool(parts & {"align", "aligned", "generate", "generated", "spatial", "judge"})


def pair_milestone(item: dict[str, Any]) -> dict[str, Any]:
    """Attach gem + worker. Do not wrap cycle/branch in judge. Do not default ok_receipt."""
    mid = str(item.get("id") or "").strip()
    if not mid:
        return item
    item["gem"] = str(item.get("gem") or "").strip() or gem_path(mid)
    kind = asset_kind(item)

    if item.get("branch") and isinstance(item.get("branch"), dict):
        br = item["branch"]
        br.setdefault("worker", item.get("worker") or "branch_receipt")
        item["worker"] = br["worker"]
        return item
    if item.get("cycle") and isinstance(item.get("cycle"), dict):
        cy = item["cycle"]
        cy.setdefault("worker", item.get("worker") or "cycle_receipt")
        item["worker"] = cy["worker"]
        return item

    current = str(item.get("worker") or "").strip()
    if needs_judge(item):
        item["loop"] = "judge"
        if not current or current in GENERIC_JUDGE:
            gate = pick_gate_tool(item.get("tools") or [])
            item["worker"] = gate or judge_worker_id(mid)
        item["receipt_schema"] = item.get("receipt_schema") or f"schemas/{mid}_receipt_v1.json"
        return item

    if not current or current in GENERIC_JUDGE:
        named = exist_worker(kind)
        tools = [str(t) for t in (item.get("tools") or []) if t]
        if not named and "hash_bind" in tools:
            named = "hash_bind"
        if named:
            item["worker"] = named
        elif current in GENERIC_JUDGE:
            item.pop("worker", None)
    return item
