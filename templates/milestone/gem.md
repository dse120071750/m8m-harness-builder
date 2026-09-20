# MASTER PROMPT — __TITLE__

You are responsible for completing **__TITLE__**. Start with this entire prompt,
then use the bound inputs and declared tools to produce the requested result.
This is the master prompt for milestone `__MID__`.

## Objective

__SUCCESS__

## Inputs and references

__INPUTS__

Use each input for its declared purpose. Follow the supplied specifications and
reference roles. Earlier milestones are available through these explicit bindings;
use their chosen outputs, including outputs from milestones before the immediate
predecessor. A mention in prose alone does not create an executable binding.

## Instructions

Carry out the complete task described above using the supplied domain instructions.
Preserve the specified identities, constraints, order, and reference relationships.
Resolve the inputs before calling the tools. If required input is missing, report
the missing input rather than inventing it. FlowStep notes below support this
master prompt and never replace it.

## Required output

__OUTPUTS__

Later milestones can bind the named outputs using the references above. Keep
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

## Tool versus intelligence

__CLASSIFICATION__

Tool-heavy means declared in-process product functions, not shell-heavy
workflow steps. Do not launch subprocesses or secondary workflows, recursively
discover the execution root, or hand-write M8M chosen/control artifacts. If an
approval boundary exists, the only later FlowStep is one declared in-process
`finalize`; otherwise keep working on the candidate.

Follow the FlowStep table as a guide. If the preferred tool fails,
recover within the same complete master prompt.
The runtime admits the named result against this expectation before any
semantic judge. PASS commits only that admitted result to
`chosen-output.json`; missing or invalid declared members BLOCK first.

## FlowSteps

__FLOWSTEP_SECTIONS__
