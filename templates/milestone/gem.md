# __MID__ — __TITLE__

## Master prompt

You are responsible for completing **__TITLE__**. Start with this entire prompt,
then use the bound inputs and declared tools to produce the requested result.
This is the master prompt for milestone `__MID__`.

### Objective

__SUCCESS__

### Inputs and references

__INPUTS__

Use each input for its declared purpose. Follow the supplied specifications and
reference roles. Earlier milestones are available through these explicit bindings;
use their chosen outputs, including outputs from milestones before the immediate
predecessor. A mention in prose alone does not create an executable binding.

### Instructions

Carry out the complete task described above using the supplied domain instructions.
Preserve the specified identities, constraints, order, and reference relationships.
Resolve the inputs before calling the tools. If required input is missing, report
the missing input rather than inventing it. The numbered FlowSteps below identify
each internal action and its actual tools; they never replace this master prompt.

### Deliverable

Return the named outputs listed after the FlowSteps. Later milestones can bind
them using their declared references. Keep
this milestone's ID and output IDs stable when editing its prompt or tools.

Return the complete named output bundle in its declared format. File and media
outputs must point to actual files. Do not substitute a summary or a short
description for a requested complete prompt, document, image, or other artifact.
Intelligence may draft; it does not set `ok`, `branch`, or `cycle`.

Default to `loop: none`. Produce the named outputs and satisfy their schema;
do not add hashes, revisions, proof graphs, or automatic image reviews.
Run a separate review only when it is explicitly requested and configured.

The authored milestone `success` and output schema are the machine completion
contract. This master prompt supplies the full task instructions and agrees with
that contract. The runtime owns progress and completion.

- Milestone: `__MID__`
- Primary output kind: `__KIND__`
- Gem: `references/__MID__.md`
- Separate judge binding: `__WORKER__` (used only when `loop: judge`)
__JUDGE_LINE__

### Tool versus intelligence

__CLASSIFICATION__

Reuse existing functions, scripts, CLIs, or APIs through declared bindings.
Respect their existing approval and idempotency contracts. Do not discover other
runs or hand-write M8M chosen/control artifacts. Explicit packaging can impose
additional implementation restrictions; local coordination does not require
rewriting working tools.

Follow the numbered FlowSteps. If the preferred tool fails,
recover within the same complete master prompt.
The runtime admits the named result against this expectation before any
semantic judge. PASS commits only that admitted result to
`chosen-output.json`; missing or invalid declared members BLOCK first.
