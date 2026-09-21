# One master prompt per milestone

Name newly authored milestones `milestone01`, `milestone02`, `milestone03`, and
so on (at least two digits; continue with `milestone100` when needed). Put the
descriptive task name in its master-prompt title, success text, and output names.
The milestone ID is the stable reference used throughout the harness:
`agents/milestone01.yaml`, `references/milestone01.md`, graph node `milestone01`,
and the producer in downstream input bindings.

Keep existing IDs when editing a workflow. Allocate the next unused number for
a new milestone without renumbering existing nodes; the graph, not the number,
determines execution order. Converting existing IDs requires updating the graph,
agent IDs/files, Gem paths, input bindings, control targets, and prompt references
together. Start a fresh run after that conversion.

Author each milestone's complete master prompt first, at
`references/<milestone>.md`. The existing `gem` field points to this file; there
is no second prompt document or new required YAML field. This applies to image
generation, prompt writing, data processing, tool execution, and upload work.

Write the prompt as instructions to the worker that will perform the whole
milestone. Include its role, objective, actual input bindings, reference roles,
domain instructions, constraints, and exact deliverable. Use as much detail as
the task needs. There is no mandatory prompt length. Preserve its domain-specific sections;
include the execution outline described below after the full master prompt.

Preserve a supplied master prompt in full. Adapt it only to implement the user's
explicit workflow choices. Resolve conflicting defaults during authoring rather
than sending contradictory prompts downstream. A short `success` sentence, tool
list, schema, or individual FlowStep note cannot replace the master prompt.

Then bind the existing tools and declare outputs that match the prompt. Every
milestone Markdown must include numbered FlowSteps with their tools, followed
by named outputs. These actions stay inside the same document and do not need
separate master prompts. Deterministic milestones still have a master prompt;
that does not require an extra model call before a working tool can run.

At milestone entry, the candidate task contains the full master prompt and its
path. Normal model calls, recovery calls, and wait/resume instructions begin
with the full prompt. Bind run-specific data separately; do not truncate the
prompt to one heading or rely on the preceding conversation to fill gaps.
Existing legacy executable flows remain readable; authoring validation requires
a nonempty master prompt when compiling or validating a workflow.

The master prompt describes the work. Named outputs and schema checks still
determine structural completion. Keep `loop: none` by default. Do not turn
prompt instructions into hashes, revision chains, proof graphs, or automatic
image reviews.

## Required milestone Markdown layout

Every milestone built or edited by the Builder includes:

1. Milestone ID and descriptive task title.
2. Its complete master prompt, including all domain instructions and references.
3. `FlowStep 1`, `FlowStep 2`, etc., in the declared order, each with a descriptive
   action and a `FlowStep N tools:` line naming the tools actually used.
4. Named outputs with their meaning, kind, cardinality, and downstream references.

For example, after the full master prompt in `references/milestone03.md`:

```markdown
## Execution plan — milestone03 — Generate six final images

### FlowStep 1: Read the six prompts and original master image (`read_inputs`)
FlowStep 1 tools: `load_waterfront_inputs@1.0.0`

### FlowStep 2: Generate img1–img6 sequentially (`generate_images`)
FlowStep 2 tools: `generate_waterfront_series@1.0.0`

### FlowStep 3: Collect the six actual files in order (`collect_images`)
FlowStep 3 tools: `collect_ordered_images@1.0.0`

## Named outputs
- `images`: six final images in img1–img6 order; image; many; required.
  Reference: `milestone03.images`.
```

These example tool names illustrate the layout; replace them with the existing
capabilities for the actual workflow. Do not create tools merely to match the
example or pad each FlowStep to three tools. Use one or several actual tools as
needed. If an action genuinely uses no tool, say `None` and explain how the
milestone handler performs it. Do not introduce hashes or reviews to fill a step.

The executable v4 schema still has one primary tool binding per FlowStep. Its
Markdown tools line must identify that binding; additional tools may be documented
only when that implementation actually calls them. Multiple independently
scheduled calls belong in separate declared FlowSteps. Listing tools in Markdown
does not grant access or create bindings. No new YAML field is required.

Use native `observer.title` and `observer.actions[].title/summary` for readable
action descriptions. The generator uses `execution.tool_bindings` for exact tool
references and `outputs` for deliverables. It preserves authored prompt text and
maintains a marked execution outline at the end of the same document. Edit source
metadata to update the generated outline; keep custom instructions outside its
markers. Coordination refreshes this outline after successful workflow validation.
An installed immutable package keeps its source until explicitly rebuilt.

Before delivering the harness, check that every milestone document has its full
prompt, all declared FlowSteps in order, their actual tools, and every named output.
This is authoring validation, not another runtime milestone or review call.

## Referencing an earlier milestone

Declare meaningful named outputs for each milestone. A later milestone can use
any available upstream output, not only the immediately preceding milestone.
Name both the producer and its output in the consuming master prompt, explain
the reference's role, and add the matching executable `inputs` binding.

In prose, `milestone01.master_image` means the `master_image` output of
`milestone01`. In YAML, use the existing binding form below: `from` contains
the producer ID and its declared `output_contract`, while `output` selects the
named port. The input alias is the key delivered to the handler. These bindings
resolve actual chosen outputs in the current run; no new reference registry,
hash, revision, or proof graph is needed. A prose reference alone does not bind
data, and a master-prompt file is not the milestone's output artifact.

For the waterfront example, declare `milestone01` with output contract
`waterfront_setup` and output `master_image`; declare `milestone02` with output
contract `waterfront_prompts` and JSON output `prompts` containing the flat
`img1`–`img6` object. Then `milestone03` binds both producers:

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

In native agent YAML, use `agent_id: milestone03` instead of `id`. Its master
prompt explicitly says to generate each `milestone02.prompts` entry using the
same `milestone01.master_image`. The original image does not need to be copied
through milestone02's outputs. For an output with `cardinality: many`, add
`member: <member_id>` to select one declared member. JSON object keys such as
`img1` are read from the resolved JSON value, not selected with `member`.

Declare the producer as upstream in the graph. Missing producers, mismatched
contracts, unknown output ports, and unavailable chosen outputs are binding
errors. Never satisfy a missing binding by searching another run or silently
substituting a previous image.

## Waterfront automotive example

These are three milestones with three different master prompts. The source
photography prompt illustrates the level of detail, not a universal photography
recipe for other workflows. If that source uses daylight but this workflow
explicitly requests a dry night scene, author the night version consistently.

| Milestone | Its master prompt must fully specify |
| --- | --- |
| `milestone01` — Generate the master image | Act as the automotive photographer. Combine V1's `anchor_prompt`, the selected car reference image, and car/Design Package text. Preserve the car build. Generate one complete front three-quarter car image with no person, at the requested dry waterfront roadside at night: pedestrian footway, pale concrete barrier, dark water, distant city lights, warm road lamps. Establish the fixed parking position, environment, lighting, and photographic treatment. Return the setup reference as `master_image`, distinct from `img1`. |
| `milestone02` — Generate six image prompts | Act as the series prompt writer. Use `master_image`, V1's full stored `master_prompt`, and selected car/person/package text. Return only one flat JSON object with `img1` through `img6`. Each value is a complete standalone prompt beginning “Generate an image according to the attached MASTER CONTINUITY IMAGE.” Include the full continuity instructions and V1's seven specified sections in every string. Copy their exact section definitions from V1; do not invent missing definitions. Specify all six shots and the person/clothing constraints below. |
| `milestone03` — Generate the final images | Act as the series image-generation operator. Execute `img1` through `img6` sequentially, sending each complete individual prompt with the same original `master_image`. Keep the vehicle parked and change camera position. Do not attach previous final images, person photographs, or depth maps. Return six actual images in order. After generation, invoke the existing Case I/O upload with workflow, car, person, and package IDs and optional caption, if upload belongs to this milestone. If upload is a separate milestone, give it its own master prompt. |

The six prompt assignments are:

1. `img1`: close rear view of the person walking beside the parked car; candid
   movement, face turned away.
2. `img2`: car-only rear three-quarter view.
3. `img3`: rear-facing full-body person adjusting hair near the right ear; the
   car's front corner enters the left of frame.
4. `img4`: car-only front three-quarter view.
5. `img5`: car-only side profile.
6. `img6`: close front-wheel, tire, brake, and adjacent fender detail.

The person appears only in `img1` and `img3`, wearing the same white long-sleeve
top, loose blue jeans, small black shoulder bag, and stable flat footwear.
V1's optional depth references are not execution inputs. The `car-girl` tag is
metadata, not an engine selector. The generator must receive the full individual
prompt; a short shot label from this guide is not a substitute.
