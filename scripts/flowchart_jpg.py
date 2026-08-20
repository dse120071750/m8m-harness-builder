"""Draw the portable audit JPEG: canvas + inside one milestone."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowstep_runtime import normalize_flowsteps
from humanize_chart import focus_milestone, humanize_flowstep, humanize_milestone


NAVY = (27, 58, 75)
BLUE = (214, 230, 245)
BLUE_EDGE = (91, 141, 184)
YELLOW = (245, 215, 110)
YELLOW_EDGE = (180, 140, 20)
GREEN = (46, 125, 50)
GREEN_BG = (232, 245, 233)
RED = (183, 28, 28)
RED_BG = (255, 235, 238)
BG = (247, 248, 250)
WHITE = (255, 255, 255)
GRAY = (90, 98, 110)

STATUS_FILL = {
    "DONE": GREEN_BG,
    "BLOCKED": RED_BG,
    "PENDING": WHITE,
}
STATUS_EDGE = {
    "DONE": GREEN,
    "BLOCKED": RED,
    "PENDING": NAVY,
}


def _font(size: int):
    from PIL import ImageFont

    for name in (
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        path = Path(name)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text(draw, xy, text, font, fill=NAVY):
    draw.text(xy, text, font=font, fill=fill)


def _box(draw, xy, fill, outline, radius=16, width=3):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _measure(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        tw, _ = _measure(draw, trial, font)
        if tw <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _center(draw, text, font, cx, cy, fill=NAVY):
    tw, th = _measure(draw, text, font)
    _text(draw, (cx - tw / 2, cy - th / 2), text, font, fill)


def _arrow(draw, x1, y, x2):
    draw.line((x1, y, x2 - 10, y), fill=NAVY, width=8)
    draw.polygon([(x2 - 14, y - 9), (x2, y), (x2 - 14, y + 9)], fill=NAVY)


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
        asset = item.get("asset") if isinstance(item.get("asset"), dict) else {}
        kind = str(item.get("asset_kind") or asset.get("kind") or "")
        flowsteps, _ = normalize_flowsteps(flowsteps=item.get("flowsteps"), tools=item.get("tools"))
        nodes.append(
            {
                "id": mid,
                "loop": str(item.get("loop") or "none"),
                "ledger": ledger,
                "asset": asset,
                "asset_kind": kind,
                "flowsteps": flowsteps,
                "status": str(statuses.get(mid) or item.get("status") or ""),
                "branch": item.get("branch") if isinstance(item.get("branch"), dict) else None,
                "on_path": item.get("on_path"),
                "cycle": item.get("cycle") if isinstance(item.get("cycle"), dict) else None,
                "success": item.get("success"),
            }
        )
    return nodes


def render_flowchart_image(
    items: list[dict[str, Any]],
    *,
    title: str,
    focus_id: str | None = None,
    statuses: dict[str, str] | None = None,
) -> Any:
    from PIL import Image, ImageDraw

    nodes = _nodes(items, statuses)
    humans = [humanize_milestone(item) for item in nodes]
    focus = focus_milestone(nodes, focus_id)
    flowsteps = []
    if focus:
        flowsteps = [humanize_flowstep(fs, i) for i, fs in enumerate(focus.get("flowsteps") or [], start=1)]

    width = 1600
    count = max(len(nodes), 1)
    box_h = 118
    top_h = 360
    inner_items = max(len(flowsteps), 1) + 1  # + asset check
    sw = min(260, max(150, (width - 520) // inner_items - 8))
    height = 820
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    title_font = _font(32)
    h2_font = _font(20)
    body_font = _font(16)
    small_font = _font(14)

    _text(draw, (48, 28), "1. Canvas — milestone to milestone", title_font)
    if title:
        tw, _ = _measure(draw, title, small_font)
        _text(draw, (width - 48 - tw, 40), title, small_font, GRAY)

    y = 118
    x = 40
    rw, rh = 100, 48
    request_cy = y + 58
    _box(draw, (x, request_cy - rh / 2, x + rw, request_cy + rh / 2), WHITE, NAVY, radius=24, width=2)
    _center(draw, "request", body_font, x + rw / 2, request_cy)
    x += rw + 8
    _arrow(draw, x, request_cy, x + 36)
    x += 40
    avail = width - x - 40
    gap = 20 if count > 4 else 44
    box_w = min(340, max(140, (avail - gap * max(count - 1, 0)) // count))

    for index, (node, human) in enumerate(zip(nodes, humans)):
        bx = x + index * (box_w + gap)
        status = human.get("status") or ""
        fill = STATUS_FILL.get(status, WHITE)
        edge = STATUS_EDGE.get(status, NAVY)
        _center(draw, "this.in = previous.out", small_font, bx + box_w / 2, y - 18, GRAY)
        _box(draw, (bx, y, bx + box_w, y + box_h), fill, edge, radius=18, width=3)
        head = f"MILESTONE  {human['id']}"
        if status:
            head = f"{head}  {status}"
        _text(draw, (bx + 16, y + 12), head[: 36], small_font, GRAY)
        title_lines = _wrap(draw, human["title"], h2_font, box_w - 32)[:1]
        _text(draw, (bx + 16, y + 36), title_lines[0], h2_font)
        if node.get("branch"):
            _text(draw, (bx + 16, y + 56), "then branch", small_font, BLUE_EDGE)
        elif node.get("on_path"):
            _text(draw, (bx + 16, y + 56), f"path: {node['on_path']}"[: 28], small_font, BLUE_EDGE)
        produce = human.get("success") or human["asset"]
        produce_lines = _wrap(draw, produce, small_font, box_w - 32)[:2]
        py = y + 78 if (node.get("branch") or node.get("on_path")) else y + 68
        for line in produce_lines:
            _text(draw, (bx + 16, py), line, small_font, NAVY)
            py += 18
        if index < len(humans) - 1:
            _arrow(draw, bx + box_w + 4, request_cy, bx + box_w + gap)

    _box(draw, (width / 2 - 280, y + box_h + 28, width / 2 + 280, y + box_h + 64), RED_BG, RED, radius=8, width=2)
    _center(
        draw,
        "No asset → BLOCK. Next milestone does not start.",
        small_font,
        width / 2,
        y + box_h + 46,
        RED,
    )

    draw.line((48, top_h - 24, width - 48, top_h - 24), fill=(210, 214, 220), width=2)
    _text(draw, (48, top_h - 8), "2. Inside one milestone — FlowSteps + tools", title_font)

    inner_top = top_h + 44
    inner_bot = height - 56
    _box(draw, (40, inner_top, width - 40, inner_bot), WHITE, BLUE_EDGE, radius=22, width=3)
    if not focus:
        _text(draw, (64, inner_top + 24), "No milestones yet.", body_font)
        return image

    focus_h = humanize_milestone(focus)
    _text(draw, (64, inner_top + 16), f"MILESTONE  {focus['id']}", h2_font)
    cap = focus_h.get("success") or (focus_h["title"] + " — " + focus_h["asset"])
    _text(draw, (64, inner_top + 44), cap[: 110], small_font, GRAY)

    sh = 92
    sx = 64
    sy = inner_top + 88
    sequence = list(flowsteps)
    check = {"id": "asset_check", "title": "asset check", "tool": "", "kind": "check"}
    sequence.append(check)

    for index, fs in enumerate(sequence):
        bx = sx + index * (sw + 36)
        is_check = fs.get("kind") == "check"
        if is_check:
            _box(draw, (bx, sy, bx + sw, sy + sh), YELLOW, YELLOW_EDGE, radius=16, width=3)
            _center(draw, "asset check", h2_font, bx + sw / 2, sy + 32)
            kind = focus_h.get("kind") or "asset"
            _center(draw, f"{kind} path + sha256" if kind in {"file", "image"} else kind, small_font, bx + sw / 2, sy + 62, GRAY)
        else:
            _box(draw, (bx, sy, bx + sw, sy + sh), BLUE, BLUE_EDGE, radius=16, width=3)
            _text(draw, (bx + 14, sy + 10), f"FlowStep {index + 1}", small_font, GRAY)
            title_lines = _wrap(draw, fs.get("title") or fs.get("id") or "", h2_font, sw - 28)[:1]
            _text(draw, (bx + 14, sy + 32), title_lines[0], h2_font)
            tool = fs.get("tool") or "—"
            tag = f"tool: {tool}"
            tw, th = _measure(draw, tag[: 28], small_font)
            tag_w = min(sw - 28, tw + 20)
            _box(draw, (bx + 14, sy + sh - 34, bx + 14 + tag_w, sy + sh - 10), WHITE, NAVY, radius=10, width=1)
            _text(draw, (bx + 24, sy + sh - 30), tag[: 28], small_font)
        if index < len(sequence) - 1:
            _arrow(draw, bx + sw + 4, sy + sh / 2, bx + sw + 32)

    check_x = width - 250
    _box(draw, (check_x, sy - 8, check_x + 186, sy + 32), GREEN_BG, GREEN, radius=10, width=2)
    _text(draw, (check_x + 12, sy), "PASS → next milestone", small_font, GREEN)
    _box(draw, (check_x, sy + 44, check_x + 186, sy + 84), RED_BG, RED, radius=10, width=2)
    _text(draw, (check_x + 12, sy + 52), "missing asset → BLOCK", small_font, RED)

    _center(
        draw,
        "FlowSteps are a guide. Tool may fail → recover like a normal agent. The asset is compulsory.",
        small_font,
        width / 2,
        inner_bot + 22,
        GRAY,
    )
    return image


def write_flowchart_jpg(
    path: Path,
    items: list[dict[str, Any]],
    *,
    title: str,
    focus_id: str | None = None,
    statuses: dict[str, str] | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = render_flowchart_image(items, title=title, focus_id=focus_id, statuses=statuses)
    image.save(path, format="JPEG", quality=92)
    return path


README_DEMO = [
    {
        "id": "source_ready",
        "asset": {"kind": "file"},
        "tools": ["fetch_record", "hash_bind"],
        "flowsteps": [
            {"id": "fetch_record", "tool": "fetch_record"},
            {"id": "hash_bind", "tool": "hash_bind"},
        ],
    },
    {
        "id": "plan_frozen",
        "asset": {"kind": "json"},
        "tools": ["compact_editorial_config"],
        "flowsteps": [{"id": "compact_plan", "tool": "compact_editorial_config"}],
    },
    {
        "id": "release_packaged",
        "asset": {"kind": "file"},
        "tools": ["materialize_package"],
        "flowsteps": [{"id": "materialize_package", "tool": "materialize_package"}],
    },
]

ARTICLE_DEMO = [
    {
        "id": "source_ready",
        "asset": {"kind": "file"},
        "tools": ["normalize_source_blocks", "hash_bind"],
        "flowsteps": [
            {"id": "normalize_source_blocks", "tool": "normalize_source_blocks"},
            {"id": "hash_bind", "tool": "hash_bind"},
        ],
    },
    {
        "id": "plan_frozen",
        "asset": {"kind": "file"},
        "tools": ["hash_bind", "schema_validate"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind"},
            {"id": "schema_validate", "tool": "schema_validate"},
        ],
    },
    {
        "id": "prompts_frozen",
        "asset": {"kind": "file"},
        "tools": ["hash_bind", "schema_validate"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind"},
            {"id": "schema_validate", "tool": "schema_validate"},
        ],
    },
    {
        "id": "assets_bound",
        "asset": {"kind": "file"},
        "loop": "for",
        "ledger": {"path": "pages", "item_schema": "schemas/page_item_v1.json", "max_items": 7},
        "worker": "ledger_receipt",
        "tools": ["hash_bind", "image_size_check", "ledger_receipt"],
        "flowsteps": [
            {"id": "hash_bind", "tool": "hash_bind"},
            {"id": "image_size_check", "tool": "image_size_check"},
        ],
    },
    {
        "id": "cards_rendered",
        "asset": {"kind": "image"},
        "loop": "judge",
        "worker": "ok_receipt",
        "tools": ["render_html_shell", "footer_geometry_qa", "hash_bind", "ok_receipt"],
        "flowsteps": [
            {"id": "render_html_shell", "tool": "render_html_shell"},
            {"id": "footer_geometry_qa", "tool": "footer_geometry_qa"},
            {"id": "hash_bind", "tool": "hash_bind"},
        ],
    },
    {
        "id": "release_packaged",
        "asset": {"kind": "file"},
        "tools": ["footer_geometry_qa", "hash_bind", "materialize_package", "io_manifest"],
        "flowsteps": [
            {"id": "footer_geometry_qa", "tool": "footer_geometry_qa"},
            {"id": "hash_bind", "tool": "hash_bind"},
            {"id": "materialize_package", "tool": "materialize_package"},
            {"id": "io_manifest", "tool": "io_manifest"},
        ],
    },
]
