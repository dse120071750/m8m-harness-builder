# M8M Harness Builder

**Milestone to Milestone — 用完整主提示詞與具名輸出，串連現有工具。**
**Connect existing tools through complete milestone master prompts and named outputs.**

[繁體中文](#繁體中文) · [English](#english) · [Skill instructions](SKILL.md) · [Authoring reference](references/builder-authoring.md)

## 繁體中文

### M8M 是甚麼？

M8M Harness Builder 用來建立及修改多階段工作流程。它協調現有工具與 FlowSteps，處理輸入綁定、輸出驗證、執行進度及中斷後續跑。每個 milestone 代表一項有明確交付物的工作；不需要把每次工具呼叫都拆成 milestone，也不需要重新實作已有工具。

目前預設使用 **coordination 模式**：編譯及驗證工作流程，沿用現有實作。只有明確需要可分發或隔離執行的套件時，才使用 package 模式。

### 每個 milestone 都從自己的完整主提示詞開始

1. 新建 milestone 使用 `milestone01`、`milestone02`、`milestone03` 等穩定 ID。
2. 先在 `references/milestoneNN.md` 撰寫該階段完整、可獨立理解的 master prompt，再設定工具及輸出。
3. 以現有的 `gem` 欄位指向這份文件，不需要另一份提示詞檔案或新增 YAML 欄位。
4. 主提示詞包括角色、目標、實際輸入及參考素材用途、工作指示、限制和確切交付物。篇幅及章節按工作需要決定。
5. 執行、模型復原及等待後續跑都帶入完整主提示詞；不能只傳某個 FlowStep 的片段，也不能依賴之前的對話補足內容。

這些規則適用於圖片生成、文字撰寫、資料處理、工具執行及上傳。確定性工具階段仍有自己的主提示詞，但不需要為此額外呼叫模型。簡短的 `success` 描述、schema 或工具清單都不能取代完整主提示詞。使用者提供的長提示詞應完整保留，只按明確指定的工作流程調整。

每份 milestone Markdown 都按以下次序呈現：**完整 master prompt → 編號 FlowSteps 及各自工具 → 具名輸出**。例如，在第三階段的完整主提示詞之後：

```text
milestone03 — 生成六張成品

FlowStep 1: 讀取六份提示詞及原始主圖
FlowStep 1 tools: load_waterfront_inputs@1.0.0

FlowStep 2: 依序生成 img1–img6
FlowStep 2 tools: generate_waterfront_series@1.0.0

FlowStep 3: 按順序收集六個實際圖片檔案
FlowStep 3 tools: collect_ordered_images@1.0.0

Named outputs: images
```

以上工具名稱只示範格式，Builder 會填入實際工作流程的工具綁定，不能留下 `<>` 或為湊數建立工具。每一步可記述一個或多個實際使用的工具；執行規格仍是每個 FlowStep 一個主要 binding，額外列出的工具必須確實由該實作呼叫。獨立排程的工具呼叫應分開宣告。每個 FlowStep 不需要另一份 master prompt。

生成器保留完整已撰寫提示詞，並從來源設定更新文末的 FlowStep／工具／輸出清單。詳見[文件格式](references/master-prompts.md#required-milestone-markdown-layout)。

修改現有工作流程時保留原有 ID。插入新階段時使用下一個未用編號，不要重新編號；執行先後由 graph 決定。

### 語意輸入，結構化輸出

**每個 milestone 不需要輸入 schema；每個 milestone 都必須有結構化輸出。**

Codex session 的筆記、補充說明、修正、圖片、檔案與上游結果可以是零散或混合格式。Worker 根據完整 master prompt 理解這些內容，提取工作所需資訊。Builder 不會為每個階段建立輸入 schema，也不會先驗證輸入欄位，或額外加一個整理輸入的 milestone／模型呼叫。

`inputs` 綁定仍可指定參考素材來自哪個上游輸出；它不是固定的輸入表格。只有真正缺少必要資訊或已宣告的上游產物時，才需要處理該缺口。工具呼叫仍使用工具本身所需的參數。

輸出則保留 `output_contract`、`output_schema` 及具名 `outputs`。例如：

```json
{
  "outputs": {
    "selection": {
      "car_id": "car_123",
      "scene": "dry waterfront at night",
      "selected_images": ["/run/master.png"]
    }
  }
}
```

這是輸出格式示例；實際欄位按該 milestone 的交付物定義。完成條件是實際資料或檔案符合輸出 schema，而不是只回覆「已完成」。下游直接使用已接受的具名結果。Model 的 `draft_schema` 描述擬提交的輸出，並非輸入限制。

目前 runner 忽略舊 milestone 的 `input_schema`；新建流程不會生成輸入 schema 檔案。既有已打包流程仍使用其固定 runtime，需重新打包才採用新行為。執行器仍透過 JSON 檔案傳送明確提供的 context；這不表示會自動讀取先前整段對話。外部 API／工作流程傳輸契約及工具參數契約不受此改動影響。

### 簡化完成條件

預設 `loop: none`。每個活躍 milestone 的執行路徑**不要求雜湊、修訂鏈、證明圖或自動圖片審核**。

完成條件是所需具名輸出確實存在，並符合宣告的 schema、種類及數量。圖片或檔案必須是實際產物，不能只有一句「已完成」。只有工作流程明確要求評審時才設定 judge；圖片生成不會因此自動多出評審階段。

套件與 runtime 仍可能有內部完整性檢查；這些不會變成每個業務 milestone 必須建立的額外交付物。

### 跨 milestone 引用輸出

後續階段可引用任何已宣告的上游輸出，不限於前一個階段。例如：

| 階段 | 具名輸出 | 用途 |
| --- | --- | --- |
| `milestone01` | `master_image` | 建立原始連續性參考圖 |
| `milestone02` | `prompts` | 產生包含 `img1` 至 `img6` 的 JSON |
| `milestone03` | `images` | 同時使用以上兩項輸出，產生六張圖片 |

在提示詞中，`milestone01.master_image` 表示第一階段的 `master_image` 輸出。實際傳遞資料仍需 YAML 綁定。以下只是綁定片段，不是完整可執行的 milestone：

```yaml
id: milestone03
gem: references/milestone03.md
inputs:
  master_image:
    from: milestone01.waterfront_setup
    output: master_image
  prompts:
    from: milestone02.waterfront_prompts
    output: prompts
```

`from` 由生產者 ID 與其宣告的 `output_contract` 組成；`output` 選取具名輸出。此例的兩個 contract 分別是 `waterfront_setup` 與 `waterfront_prompts`。原生 agent YAML 使用 `agent_id`，而不是 `id`。兩個生產者都必須在 graph 中宣告為上游。

對 `cardinality: many` 的輸出，可用 `member` 選取已宣告的成員。`img1` 等 JSON key 則從解析後的 JSON 讀取，不是 `member`。綁定解析目前 run 的已選定輸出；缺失時回報錯誤，不會改用其他 run 的素材。主提示詞文件本身也不等於輸出產物。

### 範例：海旁汽車攝影的三個階段

以下是工作流程設計範例，需要接上實際圖片工具、V1 設定與 Case I/O；並非已附帶部署的攝影服務。

| Milestone | 自己的 master prompt 必須完整定義的工作 |
| --- | --- |
| `milestone01` — 生成主圖 | 結合 V1 的 `anchor_prompt`、所選汽車參考圖，以及汽車與 Design Package 文字。生成乾燥夜間海旁路邊場景：行人步道、淺色混凝土護欄、暗色海面、遠處城市燈光及暖色路燈。完整汽車以前側三分之四角度入鏡，沒有人物。固定車輛外觀、停車位置、環境與光線，輸出 `master_image`；它不是 `img1`。 |
| `milestone02` — 生成六份提示詞 | 使用原始主圖、V1 完整 `master_prompt` 及汽車／人物／套件文字，輸出單一扁平 JSON，只有 `img1` 至 `img6` 六個欄位。每個字串都是完整獨立提示詞，以 “Generate an image according to the attached MASTER CONTINUITY IMAGE.” 開始，包含完整連續性指示與 V1 指定的七個章節。 |
| `milestone03` — 生成六張成品 | 依序執行六份完整提示詞，每次都附上同一張原始 `master_image`。汽車不移動，只改變鏡頭位置。不得附上之前生成的成品、人物照片或深度圖。輸出六張有順序的實際圖片。 |

六個鏡頭分別是：人物背面行走近景、純車尾側三分之四、人物背向全身並在右耳附近整理頭髮（汽車前角從畫面左側進入）、純車前側三分之四、純車側面，以及前輪／輪胎／煞車／相鄰葉子板特寫。

人物只出現在 `img1`、`img3`，保持白色長袖上衣、寬鬆藍色牛仔褲、小黑肩袋及平底鞋。V1 的可選深度參考不參與此執行；`car-girl` 只是標籤，不選擇另一個引擎。

若需要上傳，透過現有 Case I/O 傳送六張排序圖片、workflow／car／person／package ID 及可選 caption。上傳可屬於第三階段的明確工作範圍，或另設 `milestone04`，並為它撰寫完整主提示詞。詳見[主提示詞與引用規格](references/master-prompts.md)。

### 安裝與使用

CI 使用 Python 3.12。需要 Git；各產品工具的認證及外部服務設定由該工具管理。

以下 PowerShell 指令將本 repository 安裝為 Codex skill（只適用於尚未安裝）：

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git "$env:USERPROFILE/.codex/skills/m8m-harness-builder"
Set-Location "$env:USERPROFILE/.codex/skills/m8m-harness-builder"
python -m pip install -r requirements.txt
```

其他系統可將 repository 放在 `~/.codex/skills/m8m-harness-builder`。已有安裝時保留本機修改，於該 repository 使用 `git pull --ff-only` 更新。

在 Codex 中可這樣提出需求：

> 使用 $m8m-harness-builder，在指定的專案建立海旁汽車攝影流程。沿用現有圖片工具與 Case I/O。每個階段先寫完整 master prompt，使用 milestone01、milestone02、milestone03，並綁定具名輸出。第三階段同時引用第一階段主圖及第二階段六份提示詞。不要加入雜湊、修訂鏈、證明圖或自動圖片審核。

Builder 先了解已有實作與輸入，再撰寫完整提示詞、graph、工具綁定及輸出契約。典型原生來源結構如下（`schemas/` 內容按契約需要建立）：

```text
<project>/flowsteps/flows/<flow_id>/
  SKILL.md
  agents/
    openai.yaml          # graph 與工作流程契約的來源
    milestone01.yaml
    milestone02.yaml
    milestone03.yaml
  references/
    milestone01.md       # 完整主提示詞
    milestone02.md
    milestone03.md
  schemas/
  flow.yaml              # 原生來源編譯後的產物
```

現有工具留在原有位置，所需工具綁定位於 `<project>/flowsteps/tools/<id>/`。原生工作流程修改 `agents/openai.yaml` 及相應 agent 來源，不直接修改生成的 `flow.yaml`。既有手寫 v4 工作流程仍可直接驗證及執行，不強制轉換格式。

### 編譯、驗證與執行

以下命令都從 **Builder repository 根目錄**執行。請替換範例路徑；Windows 可用 `D:/project` 等路徑。目標工作流程、工具實作與 request JSON 必須先存在。

```sh
python scripts/run_m8m.py --mode coordinate --target "/path/to/project/flowsteps/flows/waterfront_v1" --codebase "/path/to/project"
python scripts/validate_harness.py --codebase "/path/to/project" --flow-id waterfront_v1 --scope workflow
```

`coordinate` 是預設模式。它編譯及驗證定義，並更新 milestone 文件的執行清單；不會生成圖片或打包 runtime。成功回傳的 `run_command` 是該流程應使用的執行命令；已打包流程會使用自己的 `launch.py`。

對尚未打包的 coordination 工作流程，首次執行範例如下。把可變執行資料放在專案外：

```sh
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --harness-root "/path/to/m8m-runs" --request "/path/to/request.json"
```

執行器預設建立 fresh run，快取預設關閉。若工作需要模型回覆，依回傳 action 的 task、context 及 draft 路徑完成內容，再提交該 draft；不要猜測路徑或另找其他 run 的素材。

```sh
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --run-mode resume --run-dir "/path/to/exact-run"
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --run-mode resume --run-dir "/path/to/exact-run" --draft "/path/from/action/draft.json" --draft-for milestone02
```

續跑必須指定同一個 `--run-dir`。相容的工作流程修改可透過 runner 的 `--continue-after-edit milestoneNN` 接續；需要重新生成時用 `--replace-milestone milestoneNN`，它會使該階段及依賴它的結果失效。改變 graph 或輸出介面而無法相容時，建立新 run。

執行資料包括：

| 路徑 | 內容 |
| --- | --- |
| `<run>/roster.json` | `m8m_run_roster_v1` 執行進度 |
| `<run>/milestones/<milestone>/out/chosen-output.json` | 已選定具名輸出的 manifest |
| `cycles/<id>/ledger.json`（run 內） | 工作流程使用 cycle 時的記錄 |

### 何時才需要 package？

需要可分發或隔離 runtime 時才明確使用：

```sh
python scripts/run_m8m.py --mode package --target "/path/to/source-skill" --codebase "/path/to/project"
```

Package 模式驗證及安裝套件，要求完整可執行實作；只有 scaffold 或待建工具時不能宣稱已可運行。這個命令本身不會推送 GitHub，也不會部署到遠端伺服器。詳見[套件文件](references/runtime-packaging.md)。

### 開發與排查

```sh
python -m pip install -r requirements.txt pytest==9.0.1 requests==2.32.4
python -m pytest -q tests
```

CI 使用 Ubuntu 24.04、Python 3.12。常見問題：

- **缺少主提示詞**：補齊 `gem` 指向的完整 Markdown，而不是只補一句成功條件。
- **上游綁定失敗**：確認 producer、`output_contract`、具名 port 及 graph 關係一致，且目前 run 已有該輸出。
- **只有 scaffold**：接上實際工具與 handler，再驗證；編譯成功不代表外部服務已設定完成。
- **續跑資料不符**：使用 action 所屬的 exact run；不相容的流程變更使用新 run。

延伸閱讀：[編寫規格](references/builder-authoring.md) · [主提示詞](references/master-prompts.md) · [FlowStep 開發](references/flowstep-development.md) · [Milestone 規格](references/milestone.md) · [架構](references/architecture.md)

## English

### What is M8M?

M8M Harness Builder creates and edits workflows made of milestones. It coordinates existing tools and FlowSteps, binds inputs, validates outputs, tracks progress, and resumes interrupted work. Each milestone represents work with a clear deliverable; every tool call does not need its own milestone, and existing tools do not need to be rebuilt.

The default is **coordination mode**: compile and validate the workflow while reusing existing implementations. Use package mode only when a distributable or isolated runtime is explicitly needed.

### Every milestone begins with its own complete master prompt

1. Name new milestones `milestone01`, `milestone02`, `milestone03`, and so on.
2. Write the milestone's complete, standalone master prompt in `references/milestoneNN.md` before binding tools and declaring outputs.
3. Point the existing `gem` field to that document. No second prompt file or new YAML field is needed.
4. Include the worker's role, objective, actual inputs and reference roles, domain instructions, constraints, and exact deliverable. Use the length and sections the task needs.
5. Execution, model recovery, and wait/resume instructions carry the full master prompt. Do not send only one FlowStep fragment or rely on earlier conversation to fill gaps.

This applies to image generation, writing, data processing, tool execution, and uploads. Deterministic tool milestones also have master prompts, without requiring an extra model call. A short `success` sentence, schema, or tool list cannot replace the prompt. Preserve user-supplied long prompts in full, adapting them only for explicitly requested workflow choices.

Every milestone Markdown follows this order: **complete master prompt → numbered FlowSteps with their tools → named outputs**. For example, after the third milestone's full prompt:

```text
milestone03 — Generate six final images

FlowStep 1: Read the six prompts and original master image
FlowStep 1 tools: load_waterfront_inputs@1.0.0

FlowStep 2: Generate img1–img6 sequentially
FlowStep 2 tools: generate_waterfront_series@1.0.0

FlowStep 3: Collect the six actual image files in order
FlowStep 3 tools: collect_ordered_images@1.0.0

Named outputs: images
```

These tool names illustrate the format. The Builder fills in the actual workflow bindings, leaving no `<>` placeholders and inventing no tools to pad a list. A step may document one or several tools it actually uses. The executable schema still binds one primary tool per FlowStep; additional listed tools must really be called by that implementation. Independently scheduled calls should be separate declared steps. Each FlowStep does not need another master prompt.

The generator preserves the complete authored prompt and refreshes the FlowStep/tool/output outline at the end from source metadata. See the [document format](references/master-prompts.md#required-milestone-markdown-layout).

Keep existing IDs when editing workflows. Allocate the next unused number when inserting a milestone; do not renumber existing nodes. The graph determines execution order.

### Semantic inputs, structured outputs

**No milestone input schema is required. Every milestone must have structured output.**

A Codex session can contain rough notes, corrections, prose, images, files, and upstream results in mixed formats. The worker interprets that context through the complete master prompt and extracts what the task needs. The Builder does not generate or validate per-milestone input schemas, or add a normalization milestone or model call.

`inputs` bindings can still identify where an upstream reference comes from; they do not impose a fixed context form. Address missing information only when it actually blocks the work. Missing declared upstream artifacts remain dependency errors. Tool calls still use the arguments required by the tool itself.

Outputs retain `output_contract`, `output_schema`, and named `outputs`. For example:

```json
{
  "outputs": {
    "selection": {
      "car_id": "car_123",
      "scene": "dry waterfront at night",
      "selected_images": ["/run/master.png"]
    }
  }
}
```

This illustrates the output shape; define actual fields for the milestone's deliverable. Completion requires actual data or files satisfying the output schema, not just a “done” message. Downstream work consumes those accepted named results. A model's `draft_schema` describes its proposed output, not restrictions on incoming context.

The current runner ignores legacy milestone `input_schema` fields; new builds omit those files. Already packaged workflows retain their pinned runtime and must be rebuilt to adopt this behavior. The runner still transports explicitly supplied context through JSON files; it does not automatically access previous conversation history. External workflow/API transport contracts and individual tool argument contracts remain separate.

### Simple completion requirements

Use `loop: none` by default. Active milestone execution paths **do not require hashes, revision chains, proof graphs, or automatic image reviews**.

Completion requires actual named outputs that satisfy their declared schemas, kinds, and cardinalities. Images and files must exist as artifacts; a statement saying “done” is insufficient. Configure a judge only when the workflow explicitly requires review. Image generation does not automatically add a review stage.

Packaging and runtime internals may retain integrity checks. These do not become additional deliverables required from each business milestone.

### Referencing another milestone's output

A later milestone can consume any declared upstream output, not just the immediately preceding milestone:

| Milestone | Named output | Purpose |
| --- | --- | --- |
| `milestone01` | `master_image` | Establish the original continuity reference |
| `milestone02` | `prompts` | Produce JSON containing `img1` through `img6` |
| `milestone03` | `images` | Use both earlier outputs to generate six images |

In prompt prose, `milestone01.master_image` means the `master_image` output of the first milestone. Actual data transfer still requires a YAML binding. This is a binding excerpt, not a complete runnable milestone:

```yaml
id: milestone03
gem: references/milestone03.md
inputs:
  master_image:
    from: milestone01.waterfront_setup
    output: master_image
  prompts:
    from: milestone02.waterfront_prompts
    output: prompts
```

`from` combines the producer ID and its declared `output_contract`; `output` selects the named port. The contracts in this example are `waterfront_setup` and `waterfront_prompts`. Native agent YAML uses `agent_id` instead of `id`. Both producers must be declared upstream in the graph.

For an output with `cardinality: many`, use `member` to select a declared member. JSON keys such as `img1` are read from the resolved JSON value, not selected with `member`. Bindings resolve chosen outputs from the current run. Missing outputs cause an error, rather than substitution from another run. The master-prompt document itself is not the output artifact.

### Example: three-stage waterfront automotive photography

This is a workflow design example requiring actual image tools, V1 configuration, and Case I/O integration; it is not a bundled deployed photography service.

| Milestone | Work its own master prompt must fully define |
| --- | --- |
| `milestone01` — Generate the master image | Combine V1's `anchor_prompt`, the selected car reference image, and car/Design Package text. Generate a dry waterfront roadside at night: pedestrian footway, pale concrete barrier, dark water, distant city lights, and warm road lamps. Show the complete car from a front three-quarter angle with no person. Establish vehicle appearance, parking position, environment, and lighting. Return `master_image`; this setup reference is not `img1`. |
| `milestone02` — Generate six prompts | Use the original master image, V1's full `master_prompt`, and car/person/package text. Return one flat JSON object containing only `img1` through `img6`. Each string is a complete standalone prompt beginning “Generate an image according to the attached MASTER CONTINUITY IMAGE.” Include the full continuity instructions and V1's seven specified sections. |
| `milestone03` — Generate six final images | Execute the six complete prompts sequentially, attaching the same original `master_image` every time. Keep the car parked and change camera position. Do not attach previous final images, person photographs, or depth maps. Return six actual images in order. |

The six shots are: a close rear view of the person walking; a car-only rear three-quarter view; a rear-facing full-body person adjusting hair near the right ear, with the car's front corner entering the left of frame; a car-only front three-quarter view; a car-only side profile; and a front-wheel, tire, brake, and adjacent fender detail.

The person appears only in `img1` and `img3`, consistently wearing a white long-sleeve top, loose blue jeans, a small black shoulder bag, and flat footwear. V1's optional depth references are not execution inputs. The `car-girl` tag is metadata, not an engine selector.

If uploading is requested, use existing Case I/O with the six ordered images, workflow/car/person/package IDs, and an optional caption. Upload can be explicitly included in milestone03 or separated into `milestone04` with its own complete master prompt. See the [master-prompt and reference specification](references/master-prompts.md).

### Install and use

CI uses Python 3.12. Git is required. Product tools own their authentication and external service configuration.

These PowerShell commands install this repository as a Codex skill when it is not already installed:

```powershell
git clone https://github.com/dse120071750/m8m-harness-builder.git "$env:USERPROFILE/.codex/skills/m8m-harness-builder"
Set-Location "$env:USERPROFILE/.codex/skills/m8m-harness-builder"
python -m pip install -r requirements.txt
```

On other systems, place the repository at `~/.codex/skills/m8m-harness-builder`. For an existing installation, preserve local changes and update from that repository with `git pull --ff-only`.

Example request in Codex:

> Use $m8m-harness-builder to build a waterfront automotive photography workflow in the specified project. Reuse the existing image tools and Case I/O. Write a complete master prompt for every stage, use milestone01, milestone02, and milestone03, and bind named outputs. The third stage must reference both the first stage's master image and the second stage's six prompts. Do not add hashes, revision chains, proof graphs, or automatic image reviews.

The Builder inventories existing implementations and inputs, then authors the full prompts, graph, tool bindings, and output contracts. A typical native source layout is below; populate `schemas/` as the contracts require:

```text
<project>/flowsteps/flows/<flow_id>/
  SKILL.md
  agents/
    openai.yaml          # source of graph and workflow contracts
    milestone01.yaml
    milestone02.yaml
    milestone03.yaml
  references/
    milestone01.md       # complete master prompt
    milestone02.md
    milestone03.md
  schemas/
  flow.yaml              # compiled native-source artifact
```

Keep existing implementations where they are maintained; required tool bindings live under `<project>/flowsteps/tools/<id>/`. For native workflows, edit `agents/openai.yaml` and the corresponding agent source, not the generated `flow.yaml`. Existing authored v4 workflows can still be validated and executed without a mandatory format migration.

### Compile, validate, and execute

Run these commands from the **Builder repository root**. Replace example paths; Windows paths such as `D:/project` work too. The authored target workflow, tool implementations, and request JSON must already exist.

```sh
python scripts/run_m8m.py --mode coordinate --target "/path/to/project/flowsteps/flows/waterfront_v1" --codebase "/path/to/project"
python scripts/validate_harness.py --codebase "/path/to/project" --flow-id waterfront_v1 --scope workflow
```

`coordinate` is the default mode. It compiles and validates definitions and refreshes milestone execution outlines; it does not generate images or package a runtime. The returned `run_command` identifies the runner to use. Already packaged workflows use their own `launch.py`.

For an unpackaged coordination workflow, start a run as follows. Keep mutable execution data outside the project:

```sh
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --harness-root "/path/to/m8m-runs" --request "/path/to/request.json"
```

The runner starts a fresh run by default, with caching off. When model work is needed, follow the returned action's task, context, and draft paths, then submit that draft. Do not invent paths or look in another run for missing artifacts.

```sh
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --run-mode resume --run-dir "/path/to/exact-run"
python scripts/run_flow.py --execution-mode coordination --codebase "/path/to/project" --flow-id waterfront_v1 --run-mode resume --run-dir "/path/to/exact-run" --draft "/path/from/action/draft.json" --draft-for milestone02
```

Resume with the same exact `--run-dir`. Use the runner's `--continue-after-edit milestoneNN` for compatible workflow edits. Use `--replace-milestone milestoneNN` for intentional regeneration; it invalidates that milestone and its dependents. Start a fresh run for incompatible graph or output-interface changes.

Runtime records include:

| Path | Content |
| --- | --- |
| `<run>/roster.json` | Progress in `m8m_run_roster_v1` format |
| `<run>/milestones/<milestone>/out/chosen-output.json` | Manifest of chosen named outputs |
| `cycles/<id>/ledger.json` within the run | Cycle records when the workflow uses cycles |

### When to package

Explicitly select package mode when a distributable or isolated runtime is needed:

```sh
python scripts/run_m8m.py --mode package --target "/path/to/source-skill" --codebase "/path/to/project"
```

Package mode validates and installs the package and requires complete executable implementations. A scaffold or unimplemented tool is not a runnable installation. This command does not itself push to GitHub or deploy to a remote server. See [runtime packaging](references/runtime-packaging.md).

### Development and troubleshooting

```sh
python -m pip install -r requirements.txt pytest==9.0.1 requests==2.32.4
python -m pytest -q tests
```

CI runs on Ubuntu 24.04 with Python 3.12. Common issues:

- **Missing master prompt:** complete the Markdown referenced by `gem`; a success sentence is insufficient.
- **Upstream binding failure:** check the producer, `output_contract`, named port, graph relationship, and availability of the output in this run.
- **Scaffold only:** connect actual tools and handlers before validation. Successful compilation does not configure external services.
- **Resume mismatch:** use the exact run associated with the action; start a fresh run after incompatible workflow changes.

Further reading: [Authoring](references/builder-authoring.md) · [Master prompts](references/master-prompts.md) · [FlowStep development](references/flowstep-development.md) · [Milestones](references/milestone.md) · [Architecture](references/architecture.md)

---

License / 授權：[MIT](LICENSE)
