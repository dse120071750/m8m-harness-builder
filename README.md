# M8M harness builder

Default: coordinate existing tools and FlowSteps, validate milestone outputs,
and track progress. `run_m8m.py --mode coordinate` compiles and validates local
workflow edits without a runtime release, archive, or installer.
Use `--mode package` explicitly for the packaged distribution workflow described
below. See [authoring](references/builder-authoring.md) for current commands.

[![tests](https://github.com/dse120071750/m8m-harness-builder/actions/workflows/tests.yml/badge.svg)](https://github.com/dse120071750/m8m-harness-builder/actions/workflows/tests.yml)

[中文](#中文) · [English](#english)

给 Codex / Claude 用的 skill。它把一条 skill 拆成里程碑、FlowStep、工具，再写出一张表和一张流程图。

M8M 是 milestone to milestone，里程碑到里程碑。3.1 版把 Codex skill 原生文件编译成
`flowstep_flow_v4`：FlowStep 生成候选结果，runtime 先做结构 admission；只有声明
`loop: judge` 的语义关卡才调用独立 judge。通过后才写成唯一的
`chosen-output.json` 给下游读取。选择锁是 session 目录里的逻辑状态，不是 hash、lock ID 或 revision。
所有 active milestone 默认 `loop: none`，以实际命名输出和 schema 检查完成；
不自动增加 hashes、revisions、proof graphs 或图片审核。文件路径无须附带 hash。
下文出现的 hash/judge 工具只用于用户明确要求或既有外部 API 合约。

Builder 3.1 是 compiler/packager，不是产品运行时。它产出确定性的
`m8m.workflow_source_bundle.v1`，并把不可变、digest-addressed 的 runtime release
安装到目标 codebase 的 `flowsteps/flows/<flow_id>/runtime/releases/`。产品 skill
只保留指向 codebase `launch.py` 的薄入口，绝不调用安装中的 Builder。
Builder 自己不 push、不发布、不部署。

`agents/openai.yaml` 也可以声明封闭的 workflow request、configuration、result schema，
以及成功终点的精确 output binding。这些 authoring-only contracts 会进入 source bundle，
但不会塞进 `flowstep_flow_v4` 或改变 runtime chosen state。

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
| Milestone（里程碑） | 是，护栏 | 声明 `success`、output schema 和命名 output ports。结构 admission（以及可选 judge）通过后，当前候选成为唯一 chosen bundle；没有 `chosen-output.json` 就 BLOCK。 |
| FlowStep（流程步） | 指引 | 里程碑里面的原子目标，比如绑五张图、抓一条 record。优先一支工具。顺序跟表走。怎么做到，像普通 skill。 |
| Tool（工具） | 首选，可选 | Python，放在 `<repo>/flowsteps/tools/<id>/`。Builder 该开发它：已有、从 skill script promote、或 generate-new。未实现的 generate-new 只能标记 `BUILD_REQUIRED/non_runnable`，不能通过 validation 或 install。 |

这个 skill 就干这件事：

```text
认出里程碑
  → 每个里面列出 FlowStep（原子；优先一支工具）
  → 开发该工具（existing / promote / generate-new）
  → 写一张 FlowStep 表 + 一张里程碑流程图
     （markdown + 人话 JPEG；cycle / judge / branch）
  → 编译 canonical JSON、review-only flow.yaml 和 source bundle
```

`$m8m-harness-builder` 写这个拆法。名字长得像 `crop_*` 不会拒绝画图。Stub 工具可以保留作修复草图，但状态必须是 `BUILD_REQUIRED`，不能安装。里程碑 output schema 不是草图。

## 图：里程碑到里程碑，以及一关里面有什么

画布上只有里程碑，静态 workflow JSON 显示命名 output ports。FlowSteps 生成／改善当前候选，runtime admission 通过后，`loop:none` 直接提交；`loop:judge` 再由独立 judge 作语义决定。下一关只能查询 `chosen-output.json`。没有 manifest → BLOCK。

一关**里面**是 N 个 FlowStep，以及按需声明的独立 **judge**。runtime 从 YAML 的 `success + output_contract + output_schema + outputs` 派生 expectation。所有候选先经过结构 admission；`loop:none` 不调用 judge，`loop:judge` 才发送封闭 typed request。Gem 是整個 milestone 的完整 master prompt，執行時先讀全文；FlowStep 段落只是可選實作備註。

![M8M 演示：上面是里程碑画布；下面打开 source_ready，里面是 N 个 FlowStep，然后 judge 按派生 expectation 发出 pass 收据或 keep working](docs/m8m-chart.jpg)

生成 skill 时写出 `planning/m8m-flowchart.md` 和 `planning/m8m-flowchart.jpg`。开发中每改一步（`write` / `mark`）两份都重写。JPEG 给人审：可携带、好核对、不靠 mermaid。人话来自 humanizer（`source_ready` → Source is ready）。

怎么往下走：

```text
request
  → source_ready     必须选定命名 file output
      里面：FlowStep fetch_record → tool fetch_record
            FlowStep hash_bind    → tool hash_bind
            然后 judge 读 runtime 派生的 typed expectation
            PASS → chosen-output.json → 下一关。RETRY → session 继续做。
  → plan_frozen      必须交出 json plan
  → release_packaged 必须交出 file package
```

| 种类 | chosen bundle 成员 |
| --- | --- |
| `file` | runtime 复制到 `out/members/<id>/asset.<ext>` |
| `image` / `video` / `audio` | 同样复制媒体 bytes，可在 canvas preview |
| `json` / `data` | 写成 JSON asset，并按 output schema 验证 |

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

这份仓库实现的是本机可审计、标准化的 M8M authoring、compile 和 workflow-state 合约。节点是关卡，不是一次 HTTP；来源已就绪必须交出文件，卡片已对齐会重试到 judge PASS。server Codex SDK、tenant binding、scheduler 和远端 execution 属于平台后续工作，不是 Builder 3 的现有能力或保证。

| n8n | M8M |
| --- | --- |
| 节点 = 一次 HTTP / 一次 crop | 节点 = 一个里程碑。动作在关卡**里面**（FlowStep + 工具） |
| Retry 同一节点 | **judge**：只在显式 `loop:judge` 关卡内重试。worker 返回 `{decision,reasons,blockers}`；PASS 后提交 chosen bundle。 |
| IF / Switch 节点 | **branch**：**这一关之后**选路。AI 起草，工具写 `{ok, branch}`。另一条路 skipped |
| Loop Over Items / Split in Batches | **cycle**：先冻账本，再**包一圈关卡**。每一轮 AI 起草 pass/fail，工具改账本。pass 保留；fail 清 residual，可 resume |

每一关都有一份只指导 FlowStep 的 gem。gem 不是画布节点。judge 也不是第二关；大多数确定性关卡只用 admission，只有作者明确声明的语义关卡才使用 `loop: judge` 和独立 `<id>_judge`。cycle / branch 仍用自己的控制收据。

模型不能填 `ok` / `branch` / `cycle`。收据 ok 仍不能免掉 required outputs 和 chosen manifest。

### wait — 回复已就绪

等待回复是**一关**，不是 n8n 的 Wait 节点，也不叫 `loop: wait`。里面仍是 N 个 FlowStep + 一支按需声明的 judge。session 一开始就冻一份**名册** `<run>/roster.json`（`m8m_run_roster_v1`），每关走完改一行。还没有 draft → 该行走 `waiting`、run 变 `paused`、session **退出**。人把回复写进 `milestones/response_ready/work/draft.json` 之后，skill 找到这份名册 resume；FlowStep 按 Gem 处理回复，judge 只读取 runtime 派生的 expectation 和当前 candidate，再返回 PASS、RETRY 或 BLOCKED。因为他们说了什么才选路，那是这一关 **之后** 的 branch。不要画 `roster_frozen` 关卡。名册不是 cycle 的账本 `cycles/<id>/ledger.json`。

```text
问已写下  →  回复已就绪（judge：暂停 / 继续 / pass）  →  下一关
```

```yaml
- id: response_ready
  success: "回复已经到达并可供下一关使用"
  output_contract: response_ready_v1
  output_schema: milestones/response_ready/output.schema.json
  outputs:
    - { id: reply, name: Reply, kind: json, cardinality: one, required: true }
  gem: references/response_ready.md
  intelligence: completion
  loop: judge
  worker: response_ready_judge@3.1.0
```

| 本子 | 文件 | 干什么 |
| --- | --- | --- |
| 名册（roster） | `<run>/roster.json` | 开跑就冻。每关 `done` / `waiting` / `skipped`。wait 时 `paused`，退出 session。人给回复后找这份名册 resume。 |
| 账本（cycle） | `cycles/<id>/ledger.json` | 某一圈的行。pass 留 `items/NNN`；fail 清 residual。 |

### judge — 卡片已对齐

出图、空间对齐不自动产生 judge。若 **卡片已对齐** 明确声明了语义 judge，candidate 先通过结构 admission，再由独立 judge 返回 PASS、RETRY 或 BLOCKED；只有 PASS 才提交 chosen reply。

```text
来源已就绪  →  卡片已对齐（admission → 可选 judge，直到 PASS）  →  发布包已打包
```

```yaml
- id: card_aligned
  gem: references/card_aligned.md
  loop: judge
  worker: card_aligned_judge@3.1.0
  intelligence: image
  flowsteps:
    - { id: align_compare, tool: align_compare }
    - { id: draw_red_circles, tool: draw_red_circles }
    - { id: align_edit, tool: align_edit }
    - { id: hash_bind, tool: hash_bind }
```

### branch — 入口已就绪

入口 chosen bundle 提交之后，AI 起草走哪条生成路。不要叫 IF。

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
        页已渲染     ← milestone judge PASS，然后提交 chosen output
            pass → 保留 items/001，账本该行走 done
            fail → 清掉这一轮 out/，行仍 unfinished，可再做
  → 发布包已打包
```

```yaml
- id: pages_ledger_frozen
  success: "页账本已经冻结"
  output_contract: pages_ledger_v1
  output_schema: milestones/pages_ledger_frozen/output.schema.json
  outputs:
    - { id: ledger, name: Page ledger, kind: json, cardinality: one, required: true }
- id: page_bound
  on_cycle: pages
- id: page_rendered
  on_cycle: pages
  cycle:
    worker: cycle_receipt
    ledger: pages_ledger_frozen
    start: page_bound
    join: release_packaged
    pass: "这一行走完：页图已成为 chosen output member"
```

`--milestone` 写成 `crop_4x5` 只是备注：看起来像工具。不是拒绝画图。

工具在 `<repo>/flowsteps/tools/`，不在 `~/.codex/skills` 或 `~/.claude/skills`。教学合约由作者写在 `<skill>/references/<milestone>.md`；Builder 验证后，才把 hash-bound 副本编译到 `<repo>/flowsteps/flows/<id>/references/`。

## 怎么跑

Codex（`$m8m-harness-builder`）和 Claude Code 都能用。不传 `--run-dir` 时，Windows driver 默认在 `%SystemDrive%\NisanRuntime\runs\<run-id>` 开 session（本机是 `C:\NisanRuntime`）。repo 只放 code/spec；run、goal、retry、checkpoint、milestone media 和 cache 都不能写进 repo。生成的图必须写进该树的 `address.write_to`，不要另开文件夹。

```powershell
python scripts/run_m8m.py --mode package --target <skill-or-flow-dir> --codebase <repo> --harness-root C:\NisanRuntime
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

这两条 piecemeal 命令只供审计和修 scaffold；生成的 harness 会保留
`BUILD_REQUIRED_RUNTIME`，不能执行或安装。必须完成上面的 `run_m8m.py` 五关，才会
移除 marker、安装 codebase-owned runtime release 和 product skill pointer。

会写出：

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md`：图（护栏）+ FlowStep 表（指引）+ Cycle / Judge / Branch 表
- `planning/m8m-flowchart.jpg`：人话审计 JPEG（生成时写，改一步就重写）
- `<run>/work/builder/.../source-bundle.json`：portable source bundle
- 最终 chosen `installation_receipt`：记录 bundle byte SHA、portable digest、resource root、runtime release 和本机安装清单
- 返回值 `source_bundle_path`、`source_bundle_resource_root`、`source_bundle_digest`：本机 handoff，非远端 submission
- validation chosen 之后才复制 `<repo>/flowsteps/flows/<flow_id>/`、不可变 runtime release、工具和本机 product skill target

新 run 会把当前 codebase runtime 写进自己的 `m8m-runtime-lock.json`。旧 run resume
时只认自己的 pin；即使 Builder 升级或 codebase 的 active release 已更新，也不会要求
修改产品 workflow。新版 release 并排安装，产品 skill 的 `scripts/m8m_run.py` 只指向
codebase launcher。显式 overwrite 可把旧 v1 bootstrap 在可恢复安装交易内升级到
加固后的 v2；v2 不允许原位改写，下一代 bootstrap 必须并排安装。不可变 runtime
release 不会被改写，旧 run 仍按自己的 run-local pin 执行。
产品 skill 还会固定 codebase launcher 的精确 SHA-256；任何绕过 Builder 的修改都会在启动前被拒绝。
两段 launcher 都会在导入非 built-in module 以前用 Python isolated mode 重启，
所以旁边的产品文件不能 shadow standard library；run implementation lock
覆盖已绑定工具包的 helper 与本地资源，不只 `tool.py`。每次执行都会重新枚举并
hash 完整 closure；run-local verification receipt 只能作审计证据，不能跳过检查。

Builder 本身也用同一个 v4 runtime：audit → toolbox → staged generation → validation → local install。所有五关都必须有 chosen manifest；validation chosen 以前不会写 target。`run_flow.py` 负责 schema、judge、chosen output、resume 和 `--replace-milestone`。本项目不部署。

旧 `flowstep_flow_v4` 不再自动 heuristic 转换。先用 `scripts/import_flow_v4.py`
做 inspect → digest-bound accept → stage → verify，再把通过 equivalence proof 的
skill-native stage 交给 Builder 3。Importer 只写新 staging folder，不安装、不部署。
Local install 以 managed-root snapshot + `PREPARED`/`COMMITTED` journal 做原子提升和
crash recovery，不会逐个 file 暴露半完成 target。

真实样本（一篇文章做成七页 infographic）：[examples/article_infographic/planning/m8m-flowchart.md](examples/article_infographic/planning/m8m-flowchart.md)

### Fresh rerun、resume、修 workflow、十件式 goal

一般调用和明确说 **rerun** 都默认开新 folder、`--cache-mode off`，并写
`run-context.json`：`context_policy: isolated`、`chat_history_allowed: false`。
不能把当前 Codex/Claude 对话、上一次 run、或前一件 case 的记忆偷偷当输入。
runtime 回 `ACTION_REQUIRED` 时，adapter 必须按 `context_capsule_path` 开一个
fresh no-history worker；它只能读 `allowed_files`、只能写 `write_file`。
新 run 会在 `m8m_run_context_v2` 冻结 source root、execution root、active run
和 cache root。request 里的 `file_ref_v2` 会按 SHA-256 验证、去重并只复制一次
到 `inputs/source-assets`；milestone 只收到 run-local path。ImageGen/provider
输出必须物理落在 execution volume，不能用指向 D: 的 C: junction。旧 D: run
只可用 exact `--run-dir` resume，默认 discovery 不扫描、不迁移、不删除。

```powershell
# fresh（默认）
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --request <request.json>

# 同一 run resume
python <installed-skill>/scripts/m8m_run.py --run-mode resume --run-dir <exact-run>

# 修好 workflow 后，保留 compatible upstream chosen，再从这一关继续
python <installed-skill>/scripts/m8m_run.py --run-dir <exact-run> --continue-after-edit <milestone>
```

`resume` 是同一个 run 的 chosen/attempt/roster/asset 复用，不是 cache。
`--continue-after-edit` 会审计 frozen flow 与 implementation lock，只准选择关卡
及 downstream 的改动；flow-level、关卡换序、或 preserved port 不相容就要求
fresh。明确要放弃当前 attempt 才另开，不会暗中清掉旧资产。

十件 interior case 这类 goal 用外层 ledger：

```powershell
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --goal <goal.json>
```

`goal-ledger.json` 只记每 row 的 pending/running/done/blocked；每 row 都在
`rows/<row>/attempt-NNN/run` 开独立、cache-off、无 chat history 的 child M8M
session、roster 和 milestone/cycle ledger。修 workflow 后用同一 goal folder 的
`--continue-after-edit` 继续当前 child；只有 `--abandon-row` 才保留旧 attempt
并开新 attempt。这样第十件不会继承前九件越来越长的对话。

### Workflow state 不是跨 run cache

`chosen-output.json`、attempt、roster、ledger、branch、wait、resume 和
replacement 全是一个 session 的 workflow state。跨 run cache 只是可选的
候选结果优化，短任务默认不开。要用必须两边都同意：milestone 声明
`cache: {reuse: candidate, ttl_seconds, side_effects: none}`，而新 run 传
`--cache-mode read|write|read-write`。平台还必须用 tenant ID 传
`--cache-namespace`。

cache hit 会先复制进新 run，再做 schema 验证并让**现在的 judge**重判；
不会直接成为 chosen，也不会重放旧 judge receipt。cache 坏掉、过期、
被拒或写不进去都只当 miss／warning，不能 BLOCK workflow。Resume 已完成
节点不读 cache；`--replace-milestone` 对整段失效子图绕过 cache read。
`--cache-mode write` 是明确请求的 cache-refresh run（不读、完成后可写），
不是一般「rerun」的默认。过期条目只由显式命令清理：
Generic M8M 不会把 milestone 资产导出回 repo；最终 archive 只能由 product
runtime 在验证后写入自己的 final-output 位置。

```powershell
python <installed-skill>/scripts/m8m_run.py cache-prune --harness-root C:\NisanRuntime
```

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
SKILL.md                         Builder 3 writer 工作方法（authored）
agents/                          Builder 自己的五关 agents（authored）
flows/m8m_build_v2.yaml          Builder canonical workflow（authored）
contracts/                       runtime、source-bundle、stage schemas（authored）
flowsteps/tools/                 五支可执行 Builder tools（authored）
scripts/                         compiler、runtime、validation
templates/                       product skill source templates（authored）
references/                      Builder 五关 Gems + architecture（authored）
tests/                           contract、runtime、两次 clean dogfood
examples/                        generated/reference fixtures
```

目标 skill 的 `agents/`、`references/` 和所有声明 schema 是 editable source；
目标 `flow.yaml`、flowchart、staged harness 和 source bundle 都是 generated representation。

---

# English

A Codex / Claude skill that splits another skill into milestones, FlowSteps, and tools, then writes one table and one flowchart.

M8M means milestone to milestone. Builder 3.1 compiles `flowstep_flow_v4`:
FlowSteps generate/refine a candidate, and only a judge PASS commits the
current result as the milestone's single `chosen-output.json`. This is
logical session state—not a hash lock, lock ID, or revision system.
Every active milestone defaults to `loop: none` and completes from actual named
outputs and schema checks. Hashes, revisions, proof graphs, and automatic image
reviews are not prerequisites; ordinary file paths need no checksum. Hash/judge
tools shown below apply only to an explicit request or an existing external API
contract.

Builder 3.1 is a local, deterministic compiler and packager, not a product
runtime. A skill-native canvas
must declare closed workflow request, configuration, and result schemas plus
exact terminal output bindings in `agents/openai.yaml`. Those authoring-only
contracts travel in `m8m.workflow_source_bundle.v1`; they are not injected
into the runtime graph. A successful build returns the local bundle identity
and installs an immutable, digest-addressed runtime release under the owning
codebase harness. The product skill points only to that codebase launcher and
never invokes the mutable Builder installation. The builder does not push or
deploy.

The release includes byte-locked transitive Python dependencies and runs with
system site-packages disabled; only the pinned host Python interpreter remains
external. Repository-local handler imports must be explicitly included in the
frozen implementation dependency closure, so an undeclared live helper cannot
drift underneath an existing run.

## The problem

Skills that ship assets (infographics, packages, fetches, renders) keep hitting the same bugs.

1. The model does the whole job. In the session it writes SQL, crop math, Playwright, or a one-off downloader. Work that should be typed becomes a prompt.
2. Tools live in the skill folder. Scripts sit in `~/.codex/skills` or `~/.claude/skills` instead of the product repo. The next run invents them again.
3. n8n is too stiff. Each HTTP call and crop is its own canvas node. The checkpoint a person would inspect ("source is bound", "plan is frozen") disappears under action nodes.
4. The other extreme is a dead pipeline. If a tool fails and the agent cannot recover, or if FlowSteps act like a production guardrail, the run stops for the wrong reason. Inside a checkpoint, the work is still a normal skill.

The problem is not that AI exists. The problem is using the model first, with no check on the thing that must exist, and no preferred repo tool for the thing that should be Python.

## The solution

Keep typed I/O while raising the canvas to milestones. Put FlowSteps inside each one. Each FlowStep prefers one repo tool. The builder should write that tool (fetch a table, call MCP, crop). If the tool is missing or fails, recover the way a normal agent would. Every candidate needs structural admission; only explicitly semantic milestones add a judge before chosen output.

```text
n8n:   node = one action
M8M:   node = one milestone (harness)
       this.in = previous.out
       FlowSteps = atomic goals inside that node (guide)
       Tool = the one preferred Python for a FlowStep (optional)
```

| Word | Compulsory? | Meaning |
| --- | --- | --- |
| Milestone | Yes. This is the harness. | A checkpoint with `success`, an output schema, and named output ports. Admission, plus an optional authored judge, commits the current candidate as the one chosen bundle. No chosen manifest means BLOCKED. |
| FlowStep | Guide | An atomic goal inside a milestone (bind five images, fetch a record). Prefers one tool. Follow the table order. How you get there is a normal skill. |
| Tool | Preferred, optional | Python at `<repo>/flowsteps/tools/<id>/`. The builder should write it: existing, promote from a skill script, or generate new. An unfinished generated tool is `BUILD_REQUIRED/non_runnable` and cannot pass validation or installation. |

The skill does this:

```text
identify milestones
  → list FlowSteps inside each (atomic; prefer ONE tool)
  → develop that tool (existing / promote / generate-new)
  → write one FlowStep table + one milestone flowchart
     (markdown + humanized JPEG; cycle / judge / branch)
  → compile canonical JSON, review-only flow.yaml, and a source bundle
```

`$m8m-harness-builder` writes that split. A name like `crop_*` is not a reason to refuse the chart. A stub may be kept as a repairable `BUILD_REQUIRED` sketch, but it is not runnable or installable. The milestone output schema is not a sketch.

## Chart: milestone to milestone, and what is inside one

The canvas is only milestones. Workflow JSON displays declared output ports. FlowSteps produce a current candidate; structural admission always runs, and an authored semantic judge runs only for `loop: judge`. PASS commits `chosen-output.json`, and only that manifest is visible downstream. No manifest → BLOCK.

**Inside** a milestone are N FlowSteps plus an **optional judge**. The runtime derives a typed expectation from `success + output_contract + output_schema + outputs` and admits the candidate first. `loop:none` commits with zero judge calls. `loop:judge` sends the separate judge a closed request; `RETRY` keeps work inside the node and `PASS` commits. The Gem is the complete milestone master prompt, read in full before execution. Optional FlowStep notes support it.

![M8M demo: top is the milestone canvas; bottom opens source_ready with N FlowSteps, then a judge that evaluates the derived expectation and either issues a pass receipt or tells the session to keep working](docs/m8m-chart.jpg)

Generate writes `planning/m8m-flowchart.md` and `planning/m8m-flowchart.jpg`. Every step edit during development (`write` / `mark`) rewrites both. The JPEG is the audit copy: portable, easy to review, no mermaid. Labels come from the humanizer (`source_ready` → Source is ready).

How a run proceeds:

```text
request
  → source_ready     must choose its named file output
      inside: FlowStep fetch_record → tool fetch_record
              FlowStep hash_bind    → tool hash_bind
              then the judge reads the runtime-derived typed expectation
              PASS → chosen-output.json → next. RETRY → keep working.
  → plan_frozen      must produce a json plan
  → release_packaged must produce a file package
```

| Kind | Chosen bundle member |
| --- | --- |
| `file` | copied to `out/members/<id>/asset.<ext>` |
| `image` / `video` / `audio` | copied media bytes, available for canvas preview |
| `json` / `data` | JSON asset validated by the milestone output schema |

## Table (guide)

`planning/m8m-flowchart.md` has the FlowStep table. That is the order **inside** a checkpoint, not a second canvas.

| Milestone | # | FlowStep (inside the milestone) | Preferred tool |
| --- | ---: | --- | --- |
| `source_ready` | 1 | `fetch_record` | `fetch_record` |
| `source_ready` | 2 | `hash_bind` | `hash_bind` |
| `plan_frozen` | 1 | `compact_plan` | `compact_editorial_config` |
| `release_packaged` | 1 | `materialize_package` | `materialize_package` |

The origin table says where the Python comes from: existing toolbox, promote a skill script into the repo, or generate-new. An unfinished stub counts only as a `BUILD_REQUIRED` finding.

| Milestone | Asset | Existing toolbox | Promote from a skill script | Generate new |
| --- | --- | --- | --- | --- |
| `source_ready` | `file` | `hash_bind` | `fetch_record` ← `scripts/fetch_record.py` | — |
| `plan_frozen` | `json` | — | — | `compact_editorial_config` |
| `release_packaged` | `file` | — | `materialize_package` ← `scripts/package.py` | — |

A real run on a seven-page article infographic is in [examples/article_infographic/planning/m8m-flowchart.md](examples/article_infographic/planning/m8m-flowchart.md).

## cycle / judge / branch

n8n’s canvas is actions. M8M’s canvas is checkpoints. Labels come from the humanizer (`source_ready` → Source is ready). Do not call the three rows below FOR / IF.

This repository implements local, auditable M8M authoring, compilation, and workflow-state contracts. A node is a checkpoint, not one HTTP call: Source is ready must produce a file, and Card is aligned retries until judge PASS. Server-side Codex SDK integration, tenant binding, scheduling, and remote execution are platform work outside Builder 3's current capabilities and guarantees.

| n8n | M8M |
| --- | --- |
| Node = one HTTP call / one crop | Node = one milestone. Actions sit **inside** it (FlowStep + tool) |
| Retry the same node | **judge**: only explicit `loop: judge` milestones retry. The worker returns exactly `{decision,reasons,blockers}`; the runtime persists `m8m.milestone_judge_receipt.v1`. |
| IF / Switch node | **branch**: pick a path **after this milestone**. AI drafts; the tool writes `{ok, branch}`. The other path is skipped |
| Loop Over Items / Split in Batches | **cycle**: freeze a ledger, then **wrap a stretch of milestones**. Each round AI drafts pass/fail; the tool updates the ledger. Pass preserves; fail purges residue so you can resume |

Every milestone starts with a complete master-prompt Gem: role, goal, bound inputs and reference roles, instructions, constraints, and exact output. The Gem is not a canvas node. The judge is not a second box, and Builder never infers one from a name or intelligence mode. Deterministic boxes use admission only; explicitly semantic boxes use `loop: judge` plus a named `<id>_judge`. Cycle and branch keep their own control receipts.

Every milestone has at least one required business output. Its local candidate binding is exactly `handler.<flow_id>.<milestone_id>@3.1.0`; the implementation fingerprint separately freezes the handler path and bytes, so an arbitrary ref cannot relabel the same behavior. Authored AI milestones also close the exact executor profile, Gem, schemas, tools, capabilities, token budgets, and timeouts; semantic judge milestones additionally close an independent judge profile. Missing execution terms are `BUILD_REQUIRED`; Builder 3.1 never infers a runnable server profile from chat or `intelligence`. Strict judges declare `judge_abi: m8m_milestone_judge_v1`, so the worker evaluates the current candidate separately from its producer. Audit heuristics remain review proposals only: inferred expectation or judge authority cannot emit canonical `flow.yaml`, install a skill, or execute.

The candidate executor must not set `decision` / `branch` / `cycle`. A semantic PASS or a dedicated control receipt cannot waive missing required outputs or a missing chosen manifest.

### wait — Response is ready

Wait-for-response is **one milestone**, not an n8n Wait node, and not `loop: wait`. Inside it is still N FlowSteps plus an optional authored judge. The session freezes a **roster** at start (`<run>/roster.json`, `m8m_run_roster_v1`) with one row per canvas milestone, and updates it as each box finishes. No draft yet → that row becomes `waiting`, the run `paused`, and the session **exits**. After the reply is in `milestones/response_ready/work/draft.json`, the skill finds that roster and resumes. FlowSteps use the Gem; the judge reads only the derived expectation, resolved inputs, and admitted candidate, then returns PASS, RETRY, or BLOCKED. A path because of what they said is **branch after** this box. Do not draw a `roster_frozen` milestone. Roster is not the cycle ledger at `cycles/<id>/ledger.json`.

```text
ask is written  →  Response is ready (judge: pause / keep working / pass)  →  next
```

```yaml
- id: response_ready
  success: "A reply is present and consumable downstream."
  output_contract: response_ready_v1
  output_schema: milestones/response_ready/output.schema.json
  outputs:
    - { id: reply, name: Reply, kind: json, cardinality: one, required: true }
  gem: references/response_ready.md
  intelligence: completion
  loop: judge
  worker: response_ready_judge@3.1.0
```

| Book | File | Job |
| --- | --- | --- |
| Roster | `<run>/roster.json` | Frozen when the session starts. Each milestone row is `done` / `waiting` / `skipped`. Wait sets `paused` and exits. Find this file to resume after feedback. |
| Ledger | `cycles/<id>/ledger.json` | Rows inside one wrap. Pass keeps `items/NNN`. Fail purges residue. |

### judge — Card is aligned

Image generation does not automatically imply a judge. When **Card is aligned** has an authored semantic judge, stay on it until that judge returns PASS; otherwise structural admission commits its declared output.

```text
Source is ready  →  Card is aligned (judge until PASS)  →  Release is packaged
```

```yaml
- id: card_aligned
  gem: references/card_aligned.md
  loop: judge
  worker: card_aligned_judge@3.1.0
  intelligence: image
  flowsteps:
    - { id: align_compare, tool: align_compare }
    - { id: draw_red_circles, tool: draw_red_circles }
    - { id: align_edit, tool: align_edit }
    - { id: hash_bind, tool: hash_bind }
```

### branch — Intake is ready

After the intake chosen bundle is committed, AI drafts which generation path to take. Do not call this IF.

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
        Page is rendered     ← milestone judge PASS, then commit chosen output
            pass → keep items/001, mark the ledger row done
            fail → purge this round’s out/, row stays unfinished, can redo
  → Release is packaged
```

```yaml
- id: pages_ledger_frozen
  success: "The page ledger is frozen."
  output_contract: pages_ledger_v1
  output_schema: milestones/pages_ledger_frozen/output.schema.json
  outputs:
    - { id: ledger, name: Page ledger, kind: json, cardinality: one, required: true }
- id: page_bound
  on_cycle: pages
- id: page_rendered
  on_cycle: pages
  cycle:
    worker: cycle_receipt
    ledger: pages_ledger_frozen
    start: page_bound
    join: release_packaged
    pass: "this row is done: page image is a chosen output member"
```

A name like `crop_4x5` on `--milestone` is a note that it looks like a tool. It is not a refusal to draw.

Tools belong in `<repo>/flowsteps/tools/`, not in `~/.codex/skills` or `~/.claude/skills`. Authors write teaching contracts at `<skill>/references/<milestone>.md`; after validation, Builder compiles hash-bound copies into `<repo>/flowsteps/flows/<id>/references/`.

## Run

Works in Codex (`$m8m-harness-builder`) and Claude Code. If you omit `--run-dir`, the Windows driver opens `%SystemDrive%\NisanRuntime\runs\<run-id>` (`C:\NisanRuntime` on this host). The repo stores code/specifications only; runs, goals, retries, checkpoints, milestone media, and cache must stay outside it. Generated images must be written to `address.write_to` in the execution tree.

```powershell
python scripts/run_m8m.py --mode package --target <skill-or-flow-dir> --codebase <repo> --harness-root C:\NisanRuntime
```

```powershell
python scripts/audit_harness.py --target <skill-or-flow-dir>
python scripts/generate_harness.py --codebase <repo> --from-audit <skill>/planning/flowstep-audit.json
```

Those piecemeal commands are audit/scaffold repair tools only. Their harness
keeps `BUILD_REQUIRED_RUNTIME` and cannot execute or install. Complete the
five-stage `run_m8m.py` workflow to remove the marker and install the
codebase-owned runtime release plus product-skill pointer.

This writes:

- `planning/flowstep-audit.md`
- `planning/m8m-flowchart.md`: chart (harness), FlowStep table (guide), Cycle / Judge / Branch tables
- `planning/m8m-flowchart.jpg`: humanized audit JPEG (written on generate, rewritten on every step edit)
- `<run>/work/builder/.../source-bundle.json`: the portable source bundle
- final chosen `installation_receipt`: bundle byte SHA, portable digest, resource root, runtime release, and local install manifest
- returned `source_bundle_path`, `source_bundle_resource_root`, and `source_bundle_digest`: a local handoff, not a remote submission
- `<repo>/flowsteps/flows/<flow_id>/`, immutable runtime release, tools, and the local product-skill target only after validation is chosen

Each fresh run pins the selected codebase runtime in
`m8m-runtime-lock.json`. Resume loads that exact release even after a later
Builder build changes the active release. Releases install side by side, and
the product skill's `scripts/m8m_run.py` remains only a pointer to the
codebase-owned launcher. With explicit overwrite, an old v1 bootstrap upgrades
transactionally to hardened v2. V2 is immutable in place; future bootstraps must
install side by side. Runtime releases are never rewritten, and existing runs
continue through their run-local release pin.
The product pointer also pins the exact dispatcher SHA-256 and refuses to launch
after any out-of-band dispatcher mutation.

The release also locks the Python ABI and exact runtime dependency versions.
Both bootstrap hops re-exec in Python isolated mode before importing non-builtin
modules, so sibling product files cannot shadow the standard library ahead of
verification. The dispatcher then launches an isolated process and adds only
the already-verified release scripts directory, preventing live Builder modules,
`PYTHONPATH`, or another release from leaking through `sys.modules`.
Run implementation locks cover each bound tool package's helpers and local
resources, not only `tool.py`. Every execution boundary rediscovers and hashes
that closure; run-local verification receipts never bypass the live-code check.
Python itself is a locked host prerequisite rather than a vendored interpreter;
host ABI drift fails before milestone work.

The builder dogfoods the same v4 runtime: audit → toolbox → staged generation → validation → local installation. All five milestones require chosen manifests, and the target is untouched until validation is chosen. `run_flow.py` owns schema checks, judge loops, chosen output, resume, and `--replace-milestone`. Nothing here deploys the result.

Legacy `flowstep_flow_v4` is never converted heuristically. Run
`scripts/import_flow_v4.py` through inspect → digest-bound accept → stage →
verify, then give the equivalence-proven skill-native stage to Builder 3. The
importer only creates a new staging folder; it never installs or deploys.
Local installation promotes verified managed-root snapshots through a
`PREPARED`/`COMMITTED` journal, so a crash cannot expose a file-by-file partial
target.

### Fresh rerun, resume, workflow repair, and repeated goals

An ordinary invocation and an explicit **rerun** create a new folder with
cache mode `off`. `run-context.json` freezes `context_policy: isolated` and
`chat_history_allowed: false`. Current Codex/Claude chat, another run, and a
previous case are not implicit inputs. When the runtime returns
`ACTION_REQUIRED`, the adapter must launch a fresh no-history worker from
`context_capsule_path`; it may read only `allowed_files` and write only
`write_file`.
Each new `m8m_run_context_v2` freezes the source root, execution root, active
run directory, and cache root. Request `file_ref_v2` bytes are SHA-256
verified, deduplicated, and copied once into `inputs/source-assets`; milestones
receive only run-local paths. Image provider output must physically live on
the execution volume, not behind a C: junction to D:. Legacy D: runs are
supported only by exact `--run-dir`; default discovery never scans, migrates,
or deletes them.

```powershell
# fresh (default)
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --request <request.json>

# same-run resume
python <installed-skill>/scripts/m8m_run.py --run-mode resume --run-dir <exact-run>

# adopt a workflow fix, preserve compatible upstream chosen state, rerun downstream
python <installed-skill>/scripts/m8m_run.py --run-dir <exact-run> --continue-after-edit <milestone>
```

Resume reuses one run's chosen outputs, attempts, roster, and generated
assets; it is not cache. Continue-after-edit audits the frozen flow and
implementation lock and only accepts changes owned by the selected milestone
or its downstream graph. Flow-level changes, milestone reordering, or
incompatible preserved ports require a fresh run.

Use the outer goal ledger for ten-case work:

```powershell
python <installed-skill>/scripts/m8m_run.py --harness-root C:\NisanRuntime --goal <goal.json>
```

`goal-ledger.json` tracks row status, while every row receives a distinct
cache-off, no-history child session, roster, and milestone/cycle ledger at
`rows/<row>/attempt-NNN/run`. After repairing a workflow, continue the current
child with `--continue-after-edit`. Only `--abandon-row` retains that attempt
and creates a fresh one. Later cases therefore cannot drift through an
accumulating multi-case chat.

### Workflow state is not cross-run cache

`chosen-output.json`, attempts, roster, ledger, branch, wait, resume, and
replacement are state of one session. Cross-run cache is only an optional
candidate optimization and short tasks stay uncached. It requires both a
milestone declaration (`reuse: candidate`, positive TTL, `side_effects:
none`) and an enabled run mode. Platform runs must partition it with the
tenant ID in `--cache-namespace`.

A hit is copied into the new run, schema-validated, and judged by the
current judge. It is never directly chosen and never replays an old judge
receipt. Expired, corrupt, rejected, missing, or unwritable cache becomes a
miss/warning, not BLOCKED. Resume does not consult cache for completed
milestones; replacement bypasses reads for its invalidated subgraph. Write
mode is an explicitly requested cache-refresh run that skips reads; it is not
the default meaning of “rerun.”
Generic M8M never exports milestone assets back into the repo. A product
runtime remains the sole authority for its verified final archive delivery.

```powershell
python <installed-skill>/scripts/m8m_run.py ... --cache-mode read-write --cache-namespace <tenant-or-local>
python <installed-skill>/scripts/m8m_run.py cache-prune --harness-root C:\NisanRuntime
```

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
SKILL.md                         Builder 3 writer method (authored)
agents/                          Builder's five milestone agents (authored)
flows/m8m_build_v2.yaml          Builder canonical workflow (authored)
contracts/                       runtime, source-bundle, and stage schemas (authored)
flowsteps/tools/                 five executable Builder tools (authored)
scripts/                         compiler, runtime, and validation
templates/                       product-skill source templates (authored)
references/                      five Builder Gems and architecture (authored)
tests/                           contract, runtime, and two-clean-run dogfood coverage
examples/                        generated/reference fixtures
```

For a target skill, `agents/`, `references/`, and every declared schema are
editable source. Its `flow.yaml`, flowchart, staged harness, and source bundle
are generated representations.
