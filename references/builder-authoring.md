# Coordinate an M8M workflow

The harness owns milestone order, named input/output bindings, output admission,
progress, and resume. Tools retain their existing business logic and ownership.
Default to the smallest change that implements the requested workflow.

## Choose the work

| Request | Work to perform |
| --- | --- |
| Audit | Inspect and report; do not generate or install anything. |
| Create a harness | Define milestones and bind existing tools; add adapters only where the calling contract needs one. |
| Change a prompt, binding, or milestone order | Edit the existing source and validate the affected workflow. |
| Fix a tool | Change that implementation and its relevant checks; preserve other tools. |
| Build a portable package or isolated runtime | Explicit `--mode package`; read `runtime-packaging.md`. |

Do not infer packaging from "build", "harness", "convert", "edit", or "make this
M8M". Do not run the five-stage packaging canvas for ordinary work. Missing
implementations are concrete execution blockers, not reasons to restructure
unrelated working code. Implement only what the requested task needs.

## Author and reuse

Inspect the current graph, FlowSteps, scripts, CLI commands, APIs, and tool
functions. Keep their paths and calling contracts wherever practical. Record
which capability each FlowStep invokes. A small adapter may translate typed
inputs and outputs; it must not duplicate existing business logic. Read
`flowstep-development.md` before adding one.

Assign new milestones stable IDs `milestone01`, `milestone02`, and so on. Use
the same ID in the graph, agent file, master-prompt file, and output references.
On edits, keep existing IDs and give added milestones the next unused number;
graph order determines execution. Do not renumber existing nodes to insert one.

Start each milestone by authoring its complete master prompt. Describe the
worker's role, goal, bound inputs and reference roles, domain procedure,
constraints, and exact output. This applies equally to image, text, deterministic
tool, and upload milestones. Read `master-prompts.md` for guidance and an example.
Inputs are semantic context interpreted by the worker, with no milestone
`input_schema`. Keep useful reference bindings without requiring normalized
context fields. Every milestone must still declare structured named outputs and
an output schema. See `master-prompts.md` for this boundary.
Then bind tools and outputs to that prompt. In every milestone Markdown, include
numbered FlowSteps with descriptive actions and actual tool names, followed by
named outputs. Check these lists against the executable bindings; use the layout
in `master-prompts.md`. A later milestone can consume any
available upstream milestone's named output, including several earlier milestones
at once. Declare each dependency in `inputs` and identify the same reference and
its purpose in the master prompt; see the binding examples in `master-prompts.md`.
Do not substitute the short success
sentence or separate FlowStep fragments for the full task instructions.

Native source uses `agents/openai.yaml`, one `agents/<milestone>.yaml`, and one
`references/<milestone>.md` Gem containing that master prompt per milestone. The canvas is the sole graph
authority; `flow.yaml` is regenerated output. Existing v4 schemas, named ports,
and candidate return shapes remain unchanged. An authored v4 `flow.yaml`
without a native canvas can continue directly; no forced conversion.

Author the runnable harness at `<repo>/flowsteps/flows/<flow_id>/`, the current
runner's path convention. Existing tools stay in place: Python functions,
scripts, CLIs, and APIs can be reached through thin declared bindings under
`<repo>/flowsteps/tools/<id>/`. Declare repository-local code dependencies so
edits are detected on resume; do not vendor them just to run. For a skill outside
the codebase, author only its coordination harness here and point the skill to
the resulting runner. Do not use packaging to satisfy this directory convention.

Use existing milestone checks and tool tests when adequate. Add a focused
integration check when an adapter changes how a capability is called. Do not
create new infrastructure, redundant judges, or generic adapter frameworks for
simple workflow edits.

## Compile and validate

```powershell
python scripts/run_m8m.py --target <repo>/flowsteps/flows/<flow_id> --codebase <repo>
```

`--mode coordinate` is the default. It validates source and existing
implementations, then refreshes the generated FlowStep/tool/output outlines in
milestone Markdown while preserving authored prompts. For native source it also
atomically refreshes generated `flow.yaml`. A stale snapshot does not block
recompilation; validation failure preserves the previous snapshot and prompts.
Authored v4 flow definitions keep their format. Installed packages keep their
immutable source until explicitly rebuilt.

The result contains milestone IDs and a `run_command` argument array. Validation
does not run product operations or build runtime releases, source bundles,
`.m8mpkg`, JPEGs, or installers. Flowcharts are optional review artifacts.
`--force`, `--skill-name`, and Builder session flags belong to package mode.

For validation alone:

```powershell
python scripts/validate_harness.py --codebase <repo> --flow-id <flow_id> --scope workflow
```

`workflow` validates milestone master prompts, handlers, bindings, output/draft schemas, outputs, and implementation
availability. `package` adds installed runtime verification. The Python
`validate_harness()` API retains its legacy package default for existing callers;
new coordination callers pass `scope="workflow"` explicitly.

## Run and track progress

For an unpackaged local workflow, use the returned runner with a request:

```powershell
python scripts/run_flow.py --execution-mode coordination --codebase <repo> --flow-id <flow_id> --request <request.json>
```

This reuses the installed M8M progress engine and host environment. It is local
coordination, not a portable isolated release. Existing packaged workflows use
their existing `launch.py`; coordination does not bypass workflow or run pins.
Source changes compatible with that runtime need no new release.

The engine alone writes chosen outputs, roster, receipts, and ledger. Structural
admission checks actual named outputs before completion. Default every active
path to `loop: none`, including image generation. Do not add checksum steps,
revision histories, proof graphs, or automatic image review. Ordinary file paths
need no hash companion in input, draft, tool, or output schemas. Use a separate
`loop: judge` only for an explicitly requested review. An existing external API's
checksum contract stays local to that operation.
Tools return domain evidence; they never mark their own milestone complete.

Fresh runs use isolated run-local inputs and state outside the repository.
Resume with `--run-mode resume --run-dir <exact-run>`. Supply a requested draft
through `--draft` and `--draft-for`; use a fresh bounded worker from the emitted
context capsule when model work is needed. Reuse valid chosen outputs in that
run and continue at the unfinished milestone.

After a compatible downstream implementation edit, use
`--continue-after-edit <milestone> --run-dir <exact-run>`. The engine checks which
upstream outputs remain valid. Graph reordering and incompatible contracts need
a fresh product run, not runtime packaging. Use `--replace-milestone <id>`
only for intentional regeneration within a run.

Keep bounded retry behavior and existing external-operation journals and
idempotency. Resume an external write by querying its exact operation rather than
replaying a commit. Cross-run caching remains optional and off by default.
See `milestone.md` for node design and `architecture.md` for engine details,
distinguishing packaged-release sections from local coordination.

## Explicit packaging

```powershell
python scripts/run_m8m.py --mode package --target <skill-or-flow-dir> --codebase <repo>
```

This preserves the five-stage package workflow and its closure, validation,
archive, and local installation checks. Read `runtime-packaging.md` only for
that task. Existing `m8m_factory.run_factory()` callers retain packaging behavior;
new local coordination callers use `coordinate_workflow.prepare_workflow()`.

Report changed workflow files, reused tools or added adapters, validation, and
any remaining execution blocker. Report package identities only when packaging
was requested and actually performed.
