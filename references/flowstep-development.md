# FlowStep development boundary

Classify the work before choosing an implementation. A FlowStep coordinates an
existing capability whenever it already performs the required work.

Every milestone starts from its complete master prompt in
`references/<milestone>.md`, including milestones implemented entirely by tools.
The master prompt defines the whole task; FlowSteps are the internal means of
performing it. Read the full prompt before execution or recovery. Do not replace
it with an isolated FlowStep section or add a model call just to recite it.

## Reuse tools

Deterministic work belongs in a tool: a Python function, existing script or CLI,
API client, or other declared capability. Keep its implementation in place.
Use a small typed adapter only to bridge its calling convention and the
milestone's named outputs. Do not rewrite working CLIs as in-process libraries
merely to satisfy the harness.

The current v4 runner resolves versioned tool refs through
`<repo>/flowsteps/tools/<id>/tool.py`. This can be a thin adapter; it does not
have to own or copy the underlying implementation. Retain its input/output
schemas and a meaningful adapter check. Declare local code dependencies for
resume identity; remote tools retain their own service boundary.

A CLI adapter uses a fixed executable or explicit configured path, an argument
array with `shell=False`, an explicit working directory, and a bounded timeout.
Pass structured input through stdin or a declared file; check exit status and
validate the actual reply. Do not build shell strings from model output.
An API adapter uses the existing client's authentication, timeout, error, and
idempotency behavior. A child workflow adapter records the exact child run and
resumes it instead of starting another child on each retry.

## Bound intelligence

Use intelligence when meaning, invention, comparison, or judgment cannot be
reduced to deterministic rules. Give it the needed inputs, declared tools, and
one bounded model profile. It returns a candidate draft or named outputs.
It does not own ordering, structural admission, or progress state.
Default to `loop: none`; a separate judge requires an explicit review request.
Do not infer a judge from image output, model type, or milestone name. Do not add
hashes, revision chains, proof graphs, or automatic image reviews to active
milestone paths. Named outputs and their schema are the completion contract.

For each FlowStep, identify its capability, input, output, and failure behavior.
Add a model justification or approval boundary when relevant. Avoid mandatory
analysis tables and new fixtures for unchanged working tools.

## Keep orchestration state in the engine

The runtime alone writes `chosen-output.json`, judge receipts, progress, roster,
and ledger. Tools may return business manifests and provider receipts as named
outputs. These must not impersonate runtime control artifacts. Pass exact
run-local paths; do not discover runs by searching execution storage.

Preserve the user's approval scope and existing side-effect safeguards.
Post-approval operations consume approved inputs and respect the operator's
idempotency contract. Fetching receipts or verifying readback may remain separate
FlowSteps when the existing tool requires them. Step names do not define
permission boundaries.

## Packaging-specific restrictions

Explicit `--mode package` currently admits the existing closed in-process tool
model. Its subprocess, dependency closure, and finalize restrictions remain for
compatibility; see `runtime-packaging.md`. A CLI adapter can be valid for local
coordination while unsupported by that packaging profile. Report that distinction
instead of rewriting the tool without a packaging requirement.
