"""Plain-language labels for the audit JPEG. Deterministic. No model."""

from __future__ import annotations

from typing import Any


_STATE = {
    "ready": "ready",
    "frozen": "frozen",
    "bound": "bound",
    "rendered": "rendered",
    "packaged": "packaged",
    "aligned": "aligned",
    "decided": "decided",
    "captured": "captured",
    "admitted": "admitted",
    "verified": "verified",
    "checked": "checked",
}

_COPULA_IS = {
    "source",
    "plan",
    "release",
    "card",
    "prompt",
    "package",
    "record",
    "file",
    "image",
    "harness",
    "response",
    "reply",
}

_ASSET = {
    "file": "must produce a file (path + sha256)",
    "image": "must produce an image (path + sha256)",
    "json": "must produce a json proof",
    "data": "must produce typed data",
}


def words(value: str) -> list[str]:
    return [part for part in str(value or "").replace("-", "_").split("_") if part]


def title_id(value: str) -> str:
    parts = words(value)
    if not parts:
        return "Milestone"
    if parts[-1] in _STATE and len(parts) > 1:
        head = " ".join(parts[:-1])
        last = parts[-2]
        copula = "is" if last in _COPULA_IS or not last.endswith("s") else "are"
        if last.endswith("ss"):
            copula = "is"
        return f"{head} {copula} {_STATE[parts[-1]]}".capitalize()
    return " ".join(parts).capitalize()


def asset_line(kind: str) -> str:
    return _ASSET.get(str(kind or "").lower(), "must produce the declared asset")


def _kind(node: dict[str, Any]) -> str:
    return str(
        node.get("asset_kind")
        or ((node.get("asset") or {}).get("kind") if isinstance(node.get("asset"), dict) else "")
        or ""
    )


def success_line(node: dict[str, Any]) -> str:
    """Rule of success: humanizer sentence. Explicit YAML wins."""
    explicit = str(node.get("success") or "").strip()
    if explicit:
        return explicit
    mid = str(node.get("id") or "")
    kind = _kind(node)
    loop = str(node.get("loop") or "none")
    extra = ""
    if loop == "judge":
        extra += " Retry until the worker receipt is ok."
    cycle = node.get("cycle") if isinstance(node.get("cycle"), dict) else None
    if cycle:
        declared = str(cycle.get("pass") or "").strip()
        extra += " " + (declared or "Then cycle: pass preserves the round; fail purges residue.")
    branch = node.get("branch") if isinstance(node.get("branch"), dict) else None
    if branch:
        paths = []
        for item in branch.get("paths") or []:
            if isinstance(item, dict) and item.get("id"):
                paths.append(str(item["id"]))
            elif item:
                paths.append(str(item))
        extra += f" Then branch ({' / '.join(paths)})."
    return f"{title_id(mid)} — {asset_line(kind)}.{extra}".strip()


def humanize_milestone(node: dict[str, Any]) -> dict[str, str]:
    mid = str(node.get("id") or "")
    kind = _kind(node)
    loop = str(node.get("loop") or "none")
    extra = ""
    ledger = node.get("ledger") if isinstance(node.get("ledger"), dict) else None
    if loop == "for" and ledger:
        extra = f" Walks ledger `{ledger.get('path')}` until remaining is 0."
    elif loop == "judge":
        extra = " Retry until the worker receipt is ok."
    cycle = node.get("cycle") if isinstance(node.get("cycle"), dict) else None
    if cycle:
        extra = (extra + " Then cycle: pass preserves the round and updates the ledger; fail purges residue.").strip()
    branch = node.get("branch") if isinstance(node.get("branch"), dict) else None
    if branch:
        paths = []
        for item in branch.get("paths") or []:
            if isinstance(item, dict) and item.get("id"):
                paths.append(str(item["id"]))
            elif item:
                paths.append(str(item))
        extra = (extra + f" Then branch ({' / '.join(paths)}).").strip()
    status = str(node.get("status") or "").upper()
    success = success_line(node)
    caption = f"{title_id(mid)} — {asset_line(kind)}.{extra}".strip()
    if status:
        caption = f"{caption} [{status}]"
    return {
        "id": mid,
        "title": title_id(mid),
        "asset": asset_line(kind),
        "success": success,
        "caption": caption,
        "kind": kind or "required",
        "status": status,
        "loop": loop,
    }


def humanize_flowstep(step: dict[str, Any], index: int) -> dict[str, str]:
    fid = str(step.get("id") or step.get("tool") or f"step_{index}")
    tool = str(step.get("tool") or "")
    title = title_id(fid)
    if tool:
        body = f"{title}, using tool `{tool}`"
    else:
        body = f"{title} (no preferred tool; recover like a normal agent)"
    return {"id": fid, "title": title, "tool": tool, "caption": body}


def focus_milestone(
    nodes: list[dict[str, Any]],
    focus_id: str | None = None,
) -> dict[str, Any] | None:
    if focus_id:
        for item in nodes:
            if str(item.get("id") or "") == focus_id:
                return item
    with_steps = [item for item in nodes if item.get("flowsteps")]
    if not with_steps:
        return nodes[0] if nodes else None
    def _rank(item: dict[str, Any]) -> tuple[int, int]:
        loop_bonus = 1 if str(item.get("loop") or "") == "judge" else 0
        return (len(item.get("flowsteps") or []), loop_bonus)
    ranked = sorted(with_steps, key=_rank, reverse=True)
    return ranked[0]
