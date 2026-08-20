"""简体中文人话标签。确定的。不调模型。"""

from __future__ import annotations

from typing import Any

from humanize_chart import words


_STATE = {
    "ready": "已就绪",
    "frozen": "已冻结",
    "bound": "已绑定",
    "rendered": "已渲染",
    "packaged": "已打包",
    "aligned": "已对齐",
    "decided": "已决定",
    "captured": "已捕获",
    "admitted": "已准入",
    "verified": "已核验",
    "checked": "已检查",
}

_PHRASE = {
    "restyle_direct": "直接改款",
    "floorplan_source_case": "平面图来源案",
    "fetch_record": "抓取记录",
    "hash_bind": "绑定哈希",
    "wait_for_response": "等待回复",
    "response_ready": "回复已就绪",
    "reply_ready": "回复已就绪",
}

_HEAD = {
    "source": "来源",
    "plan": "计划",
    "release": "发布包",
    "card": "卡片",
    "cards": "卡片",
    "prompt": "提示",
    "prompts": "提示",
    "package": "包裹",
    "record": "记录",
    "file": "文件",
    "image": "图片",
    "images": "图片",
    "page": "页",
    "pages": "页",
    "ledger": "账本",
    "intake": "入口",
    "restyle": "改款",
    "title": "标题",
    "floorplan": "平面图",
    "direct": "直接",
    "case": "案",
    "fetch": "抓取",
    "hash": "哈希",
    "bind": "绑定",
    "response": "回复",
    "reply": "回复",
    "confirm": "确认",
    "wait": "等待",
}

_ASSET = {
    "file": "必须交出文件（path + sha256）",
    "image": "必须交出图片（path + sha256）",
    "json": "必须交出 json 证明",
    "data": "必须交出 typed 数据",
}


def _head(parts: list[str]) -> str:
    mapped = [_HEAD.get(part, part) for part in parts]
    return "".join(mapped) if all(part in _HEAD for part in parts) else " ".join(mapped)


def title_id(value: str) -> str:
    raw = str(value or "")
    if raw in _PHRASE:
        return _PHRASE[raw]
    parts = words(value)
    if not parts:
        return "里程碑"
    if parts[-1] in _STATE and len(parts) > 1:
        return f"{_head(parts[:-1])}{_STATE[parts[-1]]}"
    return _head(parts)


def asset_line(kind: str) -> str:
    return _ASSET.get(str(kind or "").lower(), "必须交出已声明的 asset")


def success_line(node: dict[str, Any]) -> str:
    explicit = str(node.get("success") or "").strip()
    if explicit:
        return explicit
    mid = str(node.get("id") or "")
    kind = str(
        node.get("asset_kind")
        or ((node.get("asset") or {}).get("kind") if isinstance(node.get("asset"), dict) else "")
        or ""
    )
    extra = ""
    if str(node.get("loop") or "none") == "judge":
        extra = " 重试直到 worker 收据 ok。"
    cycle = node.get("cycle") if isinstance(node.get("cycle"), dict) else None
    if cycle:
        declared = str(cycle.get("pass") or "").strip()
        extra += declared or " 然后 cycle：pass 保留本轮；fail 清掉 residual。"
    branch = node.get("branch") if isinstance(node.get("branch"), dict) else None
    if branch:
        paths = []
        for item in branch.get("paths") or []:
            if isinstance(item, dict) and item.get("id"):
                paths.append(title_id(str(item["id"])))
            elif item:
                paths.append(title_id(str(item)))
        extra += f" 然后 branch（{' / '.join(paths)}）。"
    return f"{title_id(mid)} — {asset_line(kind)}。{extra}".strip()


def humanize_milestone(node: dict[str, Any]) -> dict[str, str]:
    mid = str(node.get("id") or "")
    kind = str(
        node.get("asset_kind")
        or ((node.get("asset") or {}).get("kind") if isinstance(node.get("asset"), dict) else "")
        or ""
    )
    extra = ""
    loop = str(node.get("loop") or "none")
    if loop == "judge":
        extra = " 重试直到 worker 收据 ok。"
    cycle = node.get("cycle") if isinstance(node.get("cycle"), dict) else None
    if cycle:
        extra += " 然后 cycle：pass 保留本轮并改账本；fail 清掉 residual。"
    branch = node.get("branch") if isinstance(node.get("branch"), dict) else None
    if branch:
        paths = []
        for item in branch.get("paths") or []:
            if isinstance(item, dict) and item.get("id"):
                paths.append(title_id(str(item["id"])))
            elif item:
                paths.append(title_id(str(item)))
        extra += f" 然后 branch（{' / '.join(paths)}）。"
    caption = f"{title_id(mid)} — {asset_line(kind)}。{extra}".strip()
    return {
        "id": mid,
        "title": title_id(mid),
        "asset": asset_line(kind),
        "success": success_line(node),
        "caption": caption,
        "kind": kind or "required",
    }


def humanize_flowstep(step: dict[str, Any], index: int) -> dict[str, str]:
    fid = str(step.get("id") or step.get("tool") or f"step_{index}")
    tool = str(step.get("tool") or "")
    title = title_id(fid)
    if tool:
        body = f"{title}，用工具 `{tool}`"
    else:
        body = f"{title}（没有首选工具；像普通 agent 找路）"
    return {"id": fid, "title": title, "tool": tool, "caption": body}
