# M8M harness builder

[中文](#中文) · [English](#english)

Codex / Claude **skill**：把一條 skill 拆成里程碑、FlowStep、工具，再寫出一張表、一張流程圖。

M8M = **milestone to milestone（里程碑到里程碑）**。這是輕量 skill writer，不是 production OS。

---

# 中文

## 問題

會出資產的 skill（資訊圖、包裝、抓檔、渲染）反覆栽在同一批坑：

1. **智能把整份工吃掉。** 模型在 session 裡寫 SQL、裁圖數學、Playwright、一次性 downloader。本來該是 typed 的小事，變成 prompt。
2. **工具住在 skill 資料夾。** 腳本躺在 `~/.codex/skills` / `~/.claude/skills`，不在產品 repo。下一輪又現場發明一次。
3. **n8n 太硬。** 每一次 HTTP、每一次 crop 都是畫布節點。人要檢查的關卡（「來源已綁定」「計劃已凍結」）被動作節點蓋掉。
4. **反過來的僵硬也不對。** 禁止 tool 失敗後像普通 agent 找路，或把 FlowStep 當成 production guardrail，會變成死管道。關卡**裡面**仍然是普通 skill。

過度使用不是「有 AI」。是 AI 當第一手、該存在的東西沒有護欄、該是 Python 的東西沒有首選 repo 工具。

## 解法

把 n8n 的顆粒度倒過來。保留 typed I/O。畫布升到 **Milestone（里程碑）**。裡面放 **FlowStep**。每個 FlowStep **優先一個 repo 工具**。Builder 該把這個工具做出來（抓表、叫 MCP、crop、hash）。工具沒有或失敗，就像普通 agent 找路。**里程碑產出**仍然不可談判。

```text
n8n:   節點 = 一個動作
M8M:   節點 = 一個里程碑（護欄）
       this.in = previous.out
       FlowStep = 節點裡的原子目標（指引）
       Tool = 該 FlowStep 首選的一支 Python（可選）
```

| 詞 | 硬性？ | 意思 |
| --- | --- | --- |
| **Milestone（里程碑）** | **是 — 護欄** | 像人在流程裡停下來檢查的關卡。輸入就是上一關輸出。必須交出已宣告的 **asset（產出）**：檔案、圖片、json 證明、或資料。交不出來：**BLOCK**。下一關不開始。 |
| **FlowStep（流程步）** | **指引** | 里程碑**裡面**的原子目標（綁五張圖、抓一筆 record）。**優先一支**工具。順序跟表走。怎麼做到，像普通 skill。 |
| **Tool（工具）** | **首選、可選** | Python，路徑 `<repo>/flowsteps/tools/<id>/`。Builder 該開發它。已有、從 skill script promote、或 generate-new stub。失敗就找路，目標仍是里程碑產出。 |

整份 skill 只做這件事：

```text
辨識里程碑
  → 每個裡列出 FlowStep（原子；優先 ONE 工具）
  → 開發該工具（existing / promote / generate-new）
  → 寫一張 FlowStep 表 + 一張里程碑流程圖
  → scaffold flow.yaml 與 tool stub
```

`$m8m-harness-builder` **寫**這個拆法。名字長得像 `crop_*` 不會拒絕畫圖。Stub 工具是成功的草圖。里程碑 output schema 不是草圖。

## 表（指引）

兩張表寫進 `planning/m8m-flowchart.md`。

**FlowSteps — 每個關卡裡面的順序。** 依序優先用表上的工具。可選。失敗就像普通 agent 找路。里程碑產出仍是硬性。

| Milestone | # | FlowStep | 首選工具 |
| --- | ---: | --- | --- |
| `source_ready` | 1 | `fetch_record` | `fetch_record` |
| `source_ready` | 2 | `hash_bind` | `hash_bind` |
| `plan_frozen` | 1 | `compact_plan` | `compact_editorial_config` |
| `release_packaged` | 1 | `materialize_package` | `materialize_package` |

**來源 — 這支 Python 從哪來。** 現成 toolbox、把 skill script promote 進 repo、或 generate-new（builder 該開發；stub 是草圖）。

| Milestone | Asset | 現成 toolbox | 從 skill script promote | Generate new |
| --- | --- | --- | --- | --- |
| `source_ready` | `file` | `hash_bind` | `fetch_record` ← `scripts/fetch_record.py` | — |
| `plan_frozen` | `json` | — | — | `compact_editorial_config` |
| `release_packaged` | `file` | — | `materialize_package` ← `scripts/package.py` | — |

FlowStep 跟表的順序走。不要把這條路徑當成 production lock。不要跳過產出。

## 圖（護欄）

一張 mermaid 畫布。**節點是里程碑，不是 crop。** 每個節點標必交的 asset。沒有就 BLOCKED。FlowStep 不是額外節點；它們在上面的表裡。

```mermaid
flowchart TD
    request([request]) --> source_ready
    source_ready["source_ready<br/>asset:file"] --> plan_frozen
    plan_frozen["plan_frozen<br/>asset:json<br/>intel:completion"] --> release_packaged
    release_packaged["release_packaged<br/>asset:file"]
```

| 種類 | 里程碑上的證明 |
| --- | --- |
| `file` | `asset.path` + `asset.sha256` |
| `image` | 同一套檔案回執，圖的 bytes |
| `json` | 封閉物件，必填欄位 |
| `data` | 同上：typed 必填欄位 |

If/else 與 foreach 是里程碑上可選的 **JSON Schema 閘**（`next.when` / `foreach`）。同一張圖會畫出來。模型不批准分支。

`--milestone` 寫成 `crop_4x5` 只是**註記**（「看起來像工具」），不是拒絕畫圖。

## 什麼硬、什麼不硬

| 事件 | 結果 |
| --- | --- |
| 首選 FlowStep 工具失敗 | 像普通 agent 找路（`on_tool_fail: need_model`）。先用工具。 |
| 里程碑產出缺失或不合格 | **BLOCK。** 下一關不開始。 |
| Generate-new / stub `tool.py` | Writer **PASS**。稍後再填。 |
| 里程碑上的 intelligence | 可選，用來做出產出。不可跳過首選工具。不可挑選下一個里程碑。 |

```yaml
- id: source_ready
  asset:
    kind: file
  flowsteps:
    - id: fetch_record
      tool: fetch_record
    - id: hash_bind
      tool: hash_bind
  on_tool_fail: need_model
```

工具在 `<repo>/flowsteps/tools/`，不在 `~/.codex/skills` 或 `~/.claude/skills`。教學合約在 flow 上：`<repo>/flowsteps/flows/<id>/references/`。

## 執行

**Codex**（`$m8m-harness-builder`）與 **Claude Code** 都可用。

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

產出：

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md` — mermaid（護欄）+ FlowStep 表（指引）+ 來源表
- `<repo>/flowsteps/flows/<flow_id>/`
- `<repo>/flowsteps/tools/<id>/`（seed 或 stub）
- `<repo>/.agents/skills/<name>/SKILL.md` 與 `<repo>/.claude/skills/<name>/SKILL.md`

Factory 在圖、表、stub、每個里程碑的 asset schema 都在時 **PASS**。`validate_harness.py` 可選（把工具填實）。`run_flow.py` 是護欄：工具失敗 → agent；沒有產出 → BLOCK。

## 安裝

**Codex**

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.codex\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.codex/skills/m8m-harness-builder
pip install -r ~/.codex/skills/m8m-harness-builder/requirements.txt
```

**Claude Code**

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.claude\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.claude\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.claude/skills/m8m-harness-builder
pip install -r ~/.claude/skills/m8m-harness-builder/requirements.txt
```

Repo 內：`<repo>/.agents/skills/m8m-harness-builder/` 或 `<repo>/.claude/skills/m8m-harness-builder/`。

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 目錄

```text
SKILL.md                 writer 工作方法
scripts/                 audit、generate、flowchart、可選 run/validate
templates/               stubs
examples/text_pipeline   fixture
references/              milestone + tool-vs-intelligence
```

---

# English

A Codex / Claude **skill that writes a split**: milestones, FlowSteps, tools, one table, one flowchart.

M8M = **milestone to milestone**. It is a lightweight skill writer, not a production OS.

## The problem

Skills that ship assets (infographics, packages, fetches, renders) keep failing the same way:

1. **Intelligence eats the job.** The model writes SQL, crop math, Playwright, or a one-off downloader in the session. Tiny typed work becomes a prompt.
2. **Tools live in the skill folder.** Scripts sit in `~/.codex/skills` / `~/.claude/skills` instead of the product repo. The next run invents them again.
3. **n8n is too stiff.** Every HTTP call and crop is its own canvas node. A human checkpoint (“source is bound”, “plan is frozen”) disappears under action nodes.
4. **The opposite rigidity is also wrong.** Forbidding the agent from recovering when a tool fails, or treating FlowSteps as a production guardrail, makes a dead pipeline. Inside a checkpoint, work is still a normal skill.

The overuse is not “AI exists.” It is AI used as the first move, with no harness for the thing that must exist, and no preferred repo tool for the thing that should be Python.

## The solution

Invert n8n’s grain. Keep typed I/O. Move the canvas up to **milestones**. Put **FlowSteps** inside. Prefer **one repo tool** per FlowStep. The builder develops that tool (fetch a table, call MCP, crop, hash). If the tool is missing or fails, recover like a normal agent. The **milestone asset** is still non-negotiable.

```text
n8n:   node = one action
M8M:   node = one milestone (harness)
       this.in = previous.out
       FlowSteps = atomic goals inside that node (guide)
       Tool = the one preferred Python for a FlowStep (optional)
```

| Word | Compulsory? | Meaning |
| --- | --- | --- |
| **Milestone** | **Yes — the harness** | A person-shaped checkpoint. Input is the previous output. It must produce a declared **asset** (file, image, json proof, or data). If that asset is not produced: **BLOCK**. The next milestone does not start. |
| **FlowStep** | **Guide** | An atomic goal *inside* a milestone (bind five images, fetch a record). Prefers **one** tool. Sequence comes from the table. How it gets there is a normal skill. |
| **Tool** | **Preferred, optional** | Python at `<repo>/flowsteps/tools/<id>/`. The builder should develop it. Existing, promote from a skill script, or generate-new stub. If it fails: find a way, still aimed at the milestone asset. |

This is the whole skill:

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new)
  → write one FlowStep table + one milestone flowchart
  → scaffold flow.yaml and tool stubs
```

`$m8m-harness-builder` **writes** that split. It does not refuse to draw because a name looks like `crop_*`. A stub tool is a successful sketch. The milestone output schema is not a sketch.

## Table (guide)

Two tables land on `planning/m8m-flowchart.md`.

**FlowSteps — the sequence inside each checkpoint.** Prefer the named tool, in this order. Optional. If it fails, recover like a normal agent. The milestone asset is still compulsory.

| Milestone | # | FlowStep | Preferred tool |
| --- | ---: | --- | --- |
| `source_ready` | 1 | `fetch_record` | `fetch_record` |
| `source_ready` | 2 | `hash_bind` | `hash_bind` |
| `plan_frozen` | 1 | `compact_plan` | `compact_editorial_config` |
| `release_packaged` | 1 | `materialize_package` | `materialize_package` |

**Origin — where that Python comes from.** Existing toolbox, promote a skill script into the repo, or generate-new (the builder should develop it; a stub is a sketch).

| Milestone | Asset | Existing toolbox | Promote from a skill script | Generate new |
| --- | --- | --- | --- | --- |
| `source_ready` | `file` | `hash_bind` | `fetch_record` ← `scripts/fetch_record.py` | — |
| `plan_frozen` | `json` | — | — | `compact_editorial_config` |
| `release_packaged` | `file` | — | `materialize_package` ← `scripts/package.py` | — |

Proceed FlowSteps in table order. Do not treat that path as a production lock. Do not skip the asset.

## Chart (harness)

One mermaid canvas. **Nodes are milestones, not crops.** Each node names the required asset. Missing it is BLOCKED. FlowSteps are not extra nodes; they live in the table above.

```mermaid
flowchart TD
    request([request]) --> source_ready
    source_ready["source_ready<br/>asset:file"] --> plan_frozen
    plan_frozen["plan_frozen<br/>asset:json<br/>intel:completion"] --> release_packaged
    release_packaged["release_packaged<br/>asset:file"]
```

| Kind | Proof on the milestone |
| --- | --- |
| `file` | `asset.path` + `asset.sha256` |
| `image` | same file receipt, for a picture |
| `json` | closed object with required fields |
| `data` | same: typed required fields |

If/else and foreach are optional **schema gates** on a milestone (`next.when` / `foreach`). They appear on the same chart. The model does not approve the branch.

A name like `crop_4x5` on `--milestone` is a **note** (“looks like a tool”), not a refusal to draw.

## What is rigid vs what is not

| Event | Result |
| --- | --- |
| Preferred FlowStep tool fails | Recover like a normal agent (`on_tool_fail: need_model`). Try the tool first. |
| Milestone asset missing or invalid | **BLOCK.** Next milestone does not start. |
| Generate-new / stub `tool.py` | Writer **PASS**. Fill in later. |
| Intelligence on a milestone | Optional judgment for producing the asset. Must not skip the preferred tool. Must not pick the next milestone. |

```yaml
- id: source_ready
  asset:
    kind: file
  flowsteps:
    - id: fetch_record
      tool: fetch_record
    - id: hash_bind
      tool: hash_bind
  on_tool_fail: need_model
```

Tools belong in `<repo>/flowsteps/tools/`, not in `~/.codex/skills` or `~/.claude/skills`. Teaching contracts belong on the flow: `<repo>/flowsteps/flows/<id>/references/`.

## Run

Works in **Codex** (`$m8m-harness-builder`) and **Claude Code**.

```powershell
python scripts/run_m8m.py --target <skill-or-flow-dir> --codebase <repo>
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

Deliverables:

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md` — mermaid (harness) + FlowStep table (guide) + origin table
- `<repo>/flowsteps/flows/<flow_id>/`
- `<repo>/flowsteps/tools/<id>/` (seed or stub)
- `<repo>/.agents/skills/<name>/SKILL.md` and `<repo>/.claude/skills/<name>/SKILL.md`

The factory **PASSes** when the chart, tables, stubs, and per-milestone asset schemas exist. `validate_harness.py` is optional (filling in tools). `run_flow.py` is the harness: tool fail → agent; no asset → BLOCK.

## Install

**Codex**

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git $env:USERPROFILE\.codex\skills\m8m-harness-builder
pip install -r $env:USERPROFILE\.codex\skills\m8m-harness-builder\requirements.txt
```

```bash
git clone https://github.com/dse120071750/m8m-harness-builder.git ~/.codex/skills/m8m-harness-builder
pip install -r ~/.codex/skills/m8m-harness-builder/requirements.txt
```

**Claude Code**

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
examples/text_pipeline   fixture
references/              milestone + tool-vs-intelligence
```
