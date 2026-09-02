# FlowStep development boundary

Read this Gem before designing or implementing a product FlowStep. Classify
the work before choosing an implementation.

## Dissect the work

A deterministic operation is a **tool** when a closed schema and fixture can
prove its behavior: the same typed input selects the same action, a model key
is unnecessary, and the reply is evidence rather than an opinion. Implement it
as one declared, versioned, in-process function in the product codebase.

Work is **intelligence** only when meaning, invention, comparison, or judgment
cannot be reduced to a fixture rule. Give it one bounded model profile. The
model may call only declared tools and may return only the candidate draft or
named candidate outputs. It never owns milestone order, structural admission,
judge decisions, chosen manifests, or runtime receipts.

For every FlowStep, record:

- the classification and the failed or satisfied fixture test;
- its exact typed input and named output;
- the product-codebase tool that owns deterministic work;
- the bounded model profile and justification, when intelligence is required;
- the runtime-owned evidence it must not construct; and
- the approval boundary, when present.

## Closed tool execution

Tool-heavy means typed-function-heavy, not shell-heavy. A product FlowStep may
not launch a subprocess, shell, CLI, secondary workflow, or mutable Builder
runner. The codebase launcher may bootstrap the verified pinned runtime in a
separate process; that launcher is not a FlowStep.

The closed FlowStep execution closure must not:

- run `rg` or perform recursive filesystem discovery over `C:\NisanRuntime`;
- use `Get-ChildItem -Recurse`, `os.walk`, `Path.rglob`, globstar traversal, or
  an equivalent broad search;
- use `subprocess`, `Popen`, `os.system`, `os.popen`, PowerShell, batch, or shell
  scripts as workflow steps;
- write M8M control artifacts such as `chosen-output.json`, judge receipts,
  cache receipts, progress, roster, or ledger state;
- split stability fetching or receipt generation into a second command when
  the declared tool can return the typed evidence directly; or
- perform any post-approval FlowStep other than one declared `finalize`.

`finalize` is a declared, versioned, in-process tool. It consumes an already
approved candidate and returns the final named output or external-operation
receipt. It is not permission to launch an arbitrary command. If further
searching, fetching, transformation, manifest construction, or receipt repair
is needed, approval was premature and candidate work must continue.

The runtime is the sole writer of M8M control state. A business manifest or
provider receipt may be a declared named milestone output, but it must be
validated as business data and must not impersonate a runtime control file.

## Admission consequence

The Builder treats a violation as `BUILD_REQUIRED`. A violating package may be
kept only as a non-runnable implementation sketch; it cannot pass harness
validation or authorize local installation.
