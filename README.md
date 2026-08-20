# M8M harness builder

[![tests](https://github.com/dse120071750/m8m-harness-builder/actions/workflows/tests.yml/badge.svg)](https://github.com/dse120071750/m8m-harness-builder/actions/workflows/tests.yml)

[中文](#中文) · [English](#english)

给 Codex / Claude 用的 skill。它把一条 skill 拆成里程碑、FlowStep、工具，再写出一张表和一张流程图。

M8M 是 milestone to milestone，里程碑到里程碑。它是轻量 skill writer，不是 production OS。

---

# 中文

## 问题

要出图、出包、抓文件、做渲染的 skill，老栽同一类坑。

1. 模型把活全干了。session 里写 SQL、裁图、Playwright、一次性 downloader。本来该是 typed 的小事，变成 prompt。
2. 工具住在 skill 文件夹。脚本躺在 `~/.codex/skills`、`~/.claude/skills`，不在产品 repo。下一轮又现场发明一遍。
3. n8n 太硬。一次 HTTP、一次 crop 就是一个画布节点。人要检查的关卡（来源绑好了、计划定了）被动作节点盖住。
4. 管太死也不行。工具一失败就不让 agent 找路，或者把 FlowStep 当成 production 护栏，管道就死了。关卡里面仍是普通 skill。

问题不是「用了 AI」。是 AI 当第一手，该交出来的东西没有护栏，该是 Python 的东西没有首选 repo 工具。

## 解法

把 n8n 的粒度反过来。I/O 还是 typed。画布升到 Milestone（里程碑）。里面放 FlowStep。每个 FlowStep 优先用一支 repo 工具。Builder 要把这支工具做出来：抓表、调 MCP、crop、hash。工具没有或挂了，就像普通 agent 找路。里程碑产出不能商量。

```text
n8n:   节点 = 一个动作
M8M:   节点 = 一个里程碑（护栏）
       this.in = previous.out
       FlowStep = 节点里的原子目标（指引）
       Tool = 这个 FlowStep 首选的一支 Python（可选）
```

| 词 | 硬性？ | 意思 |
| --- | --- | --- |
| Milestone（里程碑） | 是，护栏 | 人在流程里停下来检查的关卡。输入就是上一关输出。必须交出已声明的 asset：文件、图片、json 证明或数据。交不出来就 BLOCK。下一关不开始。 |
| FlowStep（流程步） | 指引 | 里程碑里面的原子目标，比如绑五张图、抓一条 record。优先一支工具。顺序跟表走。怎么做到，像普通 skill。 |
| Tool（工具） | 首选，可选 | Python，放在 `<repo>/flowsteps/tools/<id>/`。Builder 该开发它：已有、从 skill script promote、或 generate-new stub。失败就找路，目标仍是里程碑产出。 |

这个 skill 就干这件事：

```text
认出里程碑
  → 每个里面列出 FlowStep（原子；优先一支工具）
  → 开发该工具（existing / promote / generate-new）
  → 写一张 FlowStep 表 + 一张里程碑流程图
     （markdown + 人话 JPEG；cycle / judge / branch）
  → scaffold flow.yaml 和 tool stub
```

`$m8m-harness-builder` 写这个拆法。名字长得像 `crop_*` 不会拒绝画图。Stub 工具是能用的草图。里程碑 output schema 不是草图。

## 图：里程碑到里程碑，以及一关里面有什么

画布上只有里程碑。每一关必须交出声明的 asset，下一关才开始。`this.in` 就是 `previous.out`。没有 asset → BLOCK。

一关**里面**是 N 个 FlowStep，再加一支 **judge**。judge 读这一关的 gem（`references/<id>.md`）里的成功规则：合格就发 pass 收据、下一关开始；不合格就让 session 留在这一关继续做。没有 asset 仍 BLOCK。

![M8M 演示：上面是里程碑画布；下面打开 source_ready，里面是 N 个 FlowStep，然后 judge 读 references/source_ready.md，pass 收据或 keep working](docs/m8m-chart.jpg)

生成 skill 时写出 `planning/m8m-flowchart.md` 和 `planning/m8m-flowchart.jpg`。开发中每改一步（`write` / `mark`）两份都重写。JPEG 给人审：可携带、好核对、不靠 mermaid。人话来自 humanizer（`source_ready` → Source is ready）。

怎么往下走：

```text
request
  → source_ready     必须交出 file（path + sha256）
      里面：FlowStep fetch_record → tool fetch_record
            FlowStep hash_bind    → tool hash_bind
            然后 judge 读 gem references/source_ready.md
            pass 收据 → 下一关。not ok → session 继续做。没有 asset → BLOCK。
  → plan_frozen      必须交出 json plan
  → release_packaged 必须交出 file package
```

| 种类 | 这一关必须交出的证明 |
| --- | --- |
| `file` | `asset.path` + `asset.sha256` |
| `image` | 同一套文件回执，图的 bytes |
| `json` | 封闭对象，必填字段 |
| `data` | 同上：typed 必填字段 |

## 表（指引）

`planning/m8m-flowchart.md` 里有 FlowStep 表。那是关卡**里面**的顺序，不是第二张画布。

| Milestone | # | FlowStep（在关卡里面） | 首选工具 |
| --- | ---: | --- | --- |
| `source_ready` | 1 | `fetch_record` | `fetch_record` |
| `source_ready` | 2 | `hash_bind` | `hash_bind` |
| `plan_frozen` | 1 | `compact_plan` | `compact_editorial_config` |
| `release_packaged` | 1 | `materialize_package` | `materialize_package` |

来源说这支 Python 从哪来。现成 toolbox、把 skill script promote 进 repo、或 generate-new。Stub 算草图。

| Milestone | Asset | 现成 toolbox | 从 skill script promote | Generate new |
| --- | --- | --- | --- | --- |
| `source_ready` | `file` | `hash_bind` | `fetch_record` ← `scripts/fetch_record.py` | — |
| `plan_frozen` | `json` | — | — | `compact_editorial_config` |
| `release_packaged` | `file` | — | `materialize_package` ← `scripts/package.py` | — |

## cycle / judge / branch

n8n 的画布是动作。M8M 的画布是关卡。人话来自 humanizer（`source_ready` → 来源已就绪）。不要把下面三件事叫 FOR / IF。

开源部署，像 n8n：公司在内部自托管一套可审计、标准化的 agent 工作流。节点是关卡，不是一次 HTTP。来源已就绪必须交出文件；卡片已对齐会重试，直到 worker 收据 ok。n8n 是动作。Skill 是 prompt。OpenClaw 是 agent。缺的是 chat 写成 skill，再真把公司的活干完并留下证明。Codex SDK 就是那份智力：用聊天写出 M8M skill，再跑公司的活。公开仓库：m8m-harness-builder。

| n8n | M8M |
| --- | --- |
| 节点 = 一次 HTTP / 一次 crop | 节点 = 一个里程碑。动作在关卡**里面**（FlowStep + 工具） |
| Retry 同一节点 | **judge**：停在**这一关里面**，直到 asset 合格。收据 `{ok}` |
| IF / Switch 节点 | **branch**：**这一关之后**选路。AI 起草，工具写 `{ok, branch}`。另一条路 skipped |
| Loop Over Items / Split in Batches | **cycle**：先冻账本，再**包一圈关卡**。每一轮 AI 起草 pass/fail，工具改账本。pass 保留；fail 清 residual，可 resume |

每一关都有一份 gem，和一支**看这份 gem 的 worker**。gem 不是画布节点。judge 也不是第二关。存在关用 `hash_bind` / `schema_validate` 一次过；质量关才 `loop: judge` 加 `<id>_judge`（分开开发，不要共用 `ok_receipt`）。cycle / branch 仍用自己的收据。

模型不能填 `ok` / `branch` / `cycle`。收据 ok 仍不能免掉 asset。

### judge — 卡片已对齐

出图、空间对齐永远走 judge。停在 **卡片已对齐**，直到 worker 说 ok。

```text
来源已就绪  →  卡片已对齐（judge，直到 ok）  →  发布包已打包
```

```yaml
- id: card_aligned
  gem: references/card_aligned.md
  loop: judge
  worker: card_aligned_judge
  intelligence: image
```

### branch — 入口已就绪

入口 asset PASS 之后，AI 起草走哪条生成路。不要叫 IF。

```text
入口已就绪
  ├─ branch=直接改款（默认，case_type 不是 source_case）
  │     平面图来源案 skipped: true
  │     → 直接改款 → 改款已就绪
  └─ branch=平面图来源案
        要 source record + 平面图，冻标题
        → 平面图来源已就绪 → 来源标题已冻结 → 改款已就绪
```

```yaml
- id: intake_ready
  intelligence: completion
  branch:
    worker: branch_receipt
    default: direct
    paths:
      - { id: direct, then: restyle_direct }
      - { id: floorplan_source_case, then: floorplan_source_ready }
    join: restyle_ready
```

### cycle — 页账本已冻结

先冻账本，再包一圈。不要叫 FOR。`remaining == 0` 只是数据，不是闸门。

```text
页账本已冻结
  → [cycle pages]
        页已绑定
        页已渲染     ← asset PASS，然后 cycle_receipt
            pass → 保留 items/001，账本该行走 done
            fail → 清掉这一轮 out/，行仍 unfinished，可再做
  → 发布包已打包
```

```yaml
- id: pages_ledger_frozen
  asset: { kind: json }
- id: page_bound
  on_cycle: pages
- id: page_rendered
  on_cycle: pages
  cycle:
    worker: cycle_receipt
    ledger: pages_ledger_frozen
    start: page_bound
    join: release_packaged
    pass: "这一行走完：页图 path + sha256"
```

`--milestone` 写成 `crop_4x5` 只是备注：看起来像工具。不是拒绝画图。

工具在 `<repo>/flowsteps/tools/`，不在 `~/.codex/skills` 或 `~/.claude/skills`。教学合约在 flow 上：`<repo>/flowsteps/flows/<id>/references/`。

## 怎么跑

Codex（`$m8m-harness-builder`）和 Claude Code 都能用。不传 `--run-dir` 时，driver 会在 `<repo>/flowsteps/runs/<flow_id>/<时间>/` 开 session。生成的图必须写进该树的 `address.write_to`，不要另开文件夹。

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

会写出：

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md`：图（护栏）+ FlowStep 表（指引）+ Cycle / Judge / Branch 表
- `planning/m8m-flowchart.jpg`：人话审计 JPEG（生成时写，改一步就重写）
- `<repo>/flowsteps/flows/<flow_id>/`
- `<repo>/flowsteps/tools/<id>/`（seed 或 stub）
- `<repo>/.agents/skills/<name>/SKILL.md` 和 `<repo>/.claude/skills/<name>/SKILL.md`

图、表、stub、每个里程碑的 asset schema 都在，factory 就 PASS。`validate_harness.py` 可选，用来把工具填实。`run_flow.py` 是护栏：工具失败走 agent；没有产出或 worker 收据不是 ok 就 BLOCK。

真实样本（一篇文章做成七页 infographic）：[examples/article_infographic/planning/m8m-flowchart.md](examples/article_infographic/planning/m8m-flowchart.md)

## 安装

```bash
npx skills add dse120071750/m8m-harness-builder
```

Codex：

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.codex\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.codex/skills/m8m-harness-builder
pip install -r ~/.codex/skills/m8m-harness-builder/requirements.txt
```

Claude Code：

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.claude\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.claude\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.claude/skills/m8m-harness-builder
pip install -r ~/.claude/skills/m8m-harness-builder/requirements.txt
```

Repo 里：`<repo>/.agents/skills/m8m-harness-builder/` 或 `<repo>/.claude/skills/m8m-harness-builder/`。

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 目录

```text
SKILL.md                 writer 工作方法
scripts/                 audit、generate、flowchart，可选 run/validate
templates/               stubs
examples/text_pipeline           fixture
examples/article_infographic     real audit sample
references/                      milestone + tool-vs-intelligence
```

---

# English

A Codex / Claude skill that splits another skill into milestones, FlowSteps, and tools, then writes one table and one flowchart.

M8M means milestone to milestone. It is a small skill writer, not a production OS.

## The problem

Skills that ship assets (infographics, packages, fetches, renders) keep hitting the same bugs.

1. The model does the whole job. In the session it writes SQL, crop math, Playwright, or a one-off downloader. Work that should be typed becomes a prompt.
2. Tools live in the skill folder. Scripts sit in `~/.codex/skills` or `~/.claude/skills` instead of the product repo. The next run invents them again.
3. n8n is too stiff. Each HTTP call and crop is its own canvas node. The checkpoint a person would inspect ("source is bound", "plan is frozen") disappears under action nodes.
4. The other extreme is a dead pipeline. If a tool fails and the agent cannot recover, or if FlowSteps act like a production guardrail, the run stops for the wrong reason. Inside a checkpoint, the work is still a normal skill.

The problem is not that AI exists. The problem is using the model first, with no check on the thing that must exist, and no preferred repo tool for the thing that should be Python.

## The solution

Keep n8n's typed I/O. Raise the canvas to milestones. Put FlowSteps inside each one. Each FlowStep prefers one repo tool. The builder should write that tool (fetch a table, call MCP, crop, hash). If the tool is missing or fails, recover the way a normal agent would. The milestone asset still has to exist.

```text
n8n:   node = one action
M8M:   node = one milestone (harness)
       this.in = previous.out
       FlowSteps = atomic goals inside that node (guide)
       Tool = the one preferred Python for a FlowStep (optional)
```

| Word | Compulsory? | Meaning |
| --- | --- | --- |
| Milestone | Yes. This is the harness. | A checkpoint a person would inspect. Input is the previous output. It must produce a declared asset: file, image, json proof, or data. If that asset is missing, BLOCK. The next milestone does not start. |
| FlowStep | Guide | An atomic goal inside a milestone (bind five images, fetch a record). Prefers one tool. Follow the table order. How you get there is a normal skill. |
| Tool | Preferred, optional | Python at `<repo>/flowsteps/tools/<id>/`. The builder should write it: existing, promote from a skill script, or a generate-new stub. If it fails, find another way. The target is still the milestone asset. |

The skill does this:

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new)
  → write one FlowStep table + one milestone flowchart
     (markdown + humanized JPEG; cycle / judge / branch)
  → scaffold flow.yaml and tool stubs
```

`$m8m-harness-builder` writes that split. A name like `crop_*` is not a reason to refuse the chart. A stub tool is a usable sketch. The milestone output schema is not a sketch.

## Chart: milestone to milestone, and what is inside one

The canvas is only milestones. Each one must produce its declared asset before the next starts. `this.in` is `previous.out`. No asset → BLOCK.

**Inside** a milestone are N FlowSteps plus **one judge**. The judge reads this milestone’s gem (`references/<id>.md`) for the rule of success: pass receipt → next milestone; not ok → the session stays and keeps working. Missing asset still BLOCKS.

![M8M demo: top is the milestone canvas; bottom opens source_ready with N FlowSteps, then a judge that reads references/source_ready.md and either issues a pass receipt or tells the session to keep working](docs/m8m-chart.jpg)

Generate writes `planning/m8m-flowchart.md` and `planning/m8m-flowchart.jpg`. Every step edit during development (`write` / `mark`) rewrites both. The JPEG is the audit copy: portable, easy to review, no mermaid. Labels come from the humanizer (`source_ready` → Source is ready).

How a run proceeds:

```text
request
  → source_ready     must produce a file (path + sha256)
      inside: FlowStep fetch_record → tool fetch_record
              FlowStep hash_bind    → tool hash_bind
              then the judge reads gem references/source_ready.md
              pass receipt → next. not ok → session keeps working. no asset → BLOCK.
  → plan_frozen      must produce a json plan
  → release_packaged must produce a file package
```

| Kind | Proof this milestone must hand over |
| --- | --- |
| `file` | `asset.path` + `asset.sha256` |
| `image` | same file receipt, for a picture |
| `json` | closed object with required fields |
| `data` | same: typed required fields |

## Table (guide)

`planning/m8m-flowchart.md` has the FlowStep table. That is the order **inside** a checkpoint, not a second canvas.

| Milestone | # | FlowStep (inside the milestone) | Preferred tool |
| --- | ---: | --- | --- |
| `source_ready` | 1 | `fetch_record` | `fetch_record` |
| `source_ready` | 2 | `hash_bind` | `hash_bind` |
| `plan_frozen` | 1 | `compact_plan` | `compact_editorial_config` |
| `release_packaged` | 1 | `materialize_package` | `materialize_package` |

The origin table says where the Python comes from: existing toolbox, promote a skill script into the repo, or generate-new. A stub counts as a sketch.

| Milestone | Asset | Existing toolbox | Promote from a skill script | Generate new |
| --- | --- | --- | --- | --- |
| `source_ready` | `file` | `hash_bind` | `fetch_record` ← `scripts/fetch_record.py` | — |
| `plan_frozen` | `json` | — | — | `compact_editorial_config` |
| `release_packaged` | `file` | — | `materialize_package` ← `scripts/package.py` | — |

A real run on a seven-page article infographic is in [examples/article_infographic/planning/m8m-flowchart.md](examples/article_infographic/planning/m8m-flowchart.md).

## cycle / judge / branch

n8n’s canvas is actions. M8M’s canvas is checkpoints. Labels come from the humanizer (`source_ready` → Source is ready). Do not call the three rows below FOR / IF.

Deploy as open source, like n8n: companies self-host a standardized, auditable agent workflow internally. Node is a checkpoint, not one HTTP call. Source is ready must produce a file; Card is aligned retries until the worker receipt is ok. n8n is actions. Skills are prompts. OpenClaw is an agent. The missing piece is chat-to-skill plus actually doing the job with proof. Codex SDK is that intelligence: write the M8M skill in chat, then run the company's work. Public repo: m8m-harness-builder.

| n8n | M8M |
| --- | --- |
| Node = one HTTP call / one crop | Node = one milestone. Actions sit **inside** it (FlowStep + tool) |
| Retry the same node | **judge**: stay **inside this milestone** until the asset is good. Receipt `{ok}` |
| IF / Switch node | **branch**: pick a path **after this milestone**. AI drafts; the tool writes `{ok, branch}`. The other path is skipped |
| Loop Over Items / Split in Batches | **cycle**: freeze a ledger, then **wrap a stretch of milestones**. Each round AI drafts pass/fail; the tool updates the ledger. Pass preserves; fail purges residue so you can resume |

Every milestone has a gem and **one worker that looks at that gem**. The gem is not a canvas node. The judge is not a second box. Exist boxes use `hash_bind` / `schema_validate` once. Quality boxes use `loop: judge` plus a named `<id>_judge` (developed separately; not shared `ok_receipt`). Cycle and branch keep their own receipts.

The model must not set `ok` / `branch` / `cycle`. An ok receipt still cannot waive a missing asset.

### judge — Card is aligned

Image generation and spatial alignment always use judge. Stay on **Card is aligned** until the worker says ok.

```text
Source is ready  →  Card is aligned (judge until ok)  →  Release is packaged
```

```yaml
- id: card_aligned
  gem: references/card_aligned.md
  loop: judge
  worker: card_aligned_judge
  intelligence: image
```

### branch — Intake is ready

After the intake asset PASSes, AI drafts which generation path to take. Do not call this IF.

```text
Intake is ready
  ├─ branch=direct (default, case_type is not source_case)
  │     floorplan source case skipped: true
  │     → Restyle direct → Restyle is ready
  └─ branch=floorplan source case
        source record + floor plan required, freeze the title
        → Floorplan source is ready → Source title is frozen → Restyle is ready
```

```yaml
- id: intake_ready
  intelligence: completion
  branch:
    worker: branch_receipt
    default: direct
    paths:
      - { id: direct, then: restyle_direct }
      - { id: floorplan_source_case, then: floorplan_source_ready }
    join: restyle_ready
```

### cycle — Pages ledger is frozen

Freeze the ledger first, then wrap. Do not call this FOR. `remaining == 0` is data, not the gate.

```text
Pages ledger is frozen
  → [cycle pages]
        Page is bound
        Page is rendered     ← asset PASS, then cycle_receipt
            pass → keep items/001, mark the ledger row done
            fail → purge this round’s out/, row stays unfinished, can redo
  → Release is packaged
```

```yaml
- id: pages_ledger_frozen
  asset: { kind: json }
- id: page_bound
  on_cycle: pages
- id: page_rendered
  on_cycle: pages
  cycle:
    worker: cycle_receipt
    ledger: pages_ledger_frozen
    start: page_bound
    join: release_packaged
    pass: "this row is done: page image path + sha256"
```

A name like `crop_4x5` on `--milestone` is a note that it looks like a tool. It is not a refusal to draw.

Tools belong in `<repo>/flowsteps/tools/`, not in `~/.codex/skills` or `~/.claude/skills`. Teaching contracts belong on the flow: `<repo>/flowsteps/flows/<id>/references/`.

## Run

Works in Codex (`$m8m-harness-builder`) and Claude Code. If you omit `--run-dir`, the driver opens `<repo>/flowsteps/runs/<flow_id>/<timestamp>/`. Generated images must be written to `address.write_to` in that tree.

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

This writes:

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md`: chart (harness), FlowStep table (guide), Cycle / Judge / Branch tables
- `planning/m8m-flowchart.jpg`: humanized audit JPEG (written on generate, rewritten on every step edit)
- `<repo>/flowsteps/flows/<flow_id>/`
- `<repo>/flowsteps/tools/<id>/` (seed or stub)
- `<repo>/.agents/skills/<name>/SKILL.md` and `<repo>/.claude/skills/<name>/SKILL.md`

The factory PASSes when the chart, tables, stubs, and each milestone's asset schema exist. `validate_harness.py` is optional. Use it when you fill in tools. `run_flow.py` is the harness: a failed tool goes to the agent; a missing asset or a not-ok worker receipt BLOCKs.

## Install

```bash
npx skills add dse120071750/m8m-harness-builder
```

Codex:

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.codex\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.codex/skills/m8m-harness-builder
pip install -r ~/.codex/skills/m8m-harness-builder/requirements.txt
```

Claude Code:

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.claude\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.claude\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.claude/skills/m8m-harness-builder
pip install -r ~/.claude/skills/m8m-harness-builder/requirements.txt
```

Repo-local: `<repo>/.agents/skills/m8m-harness-builder/` or `<repo>/.claude/skills/m8m-harness-builder/`.

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Layout

```text
SKILL.md                 writer working method
scripts/                 audit, generate, flowchart, optional run/validate
templates/               stubs
examples/text_pipeline           fixture
examples/article_infographic     real audit sample
references/                      milestone + tool-vs-intelligence
```
