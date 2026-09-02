# Builder Flow Generated

## `compile_source_bundle`

Consume the chosen source audit and toolbox manifest. If the audit is
`authoring_mode: from_context`, author the current skill-native canvas, Gems,
handlers, and repo tools the same way as a new workflow, reusing the audit
inventory, then compile. This is not an equivalence proof; lossless v4 import
remains `scripts/import_flow_v4.py`.
Return two required JSON ports: `workflow_source_bundle` is the exact
base-independent `m8m.workflow_source_bundle.v1`, while `staged_harness` is the
separate run-local generation report with `source_bundle_path` and
`source_bundle_digest` linkage. The staged report must not embed another copy
of the source bundle. Keep every generated member inside this run's staging
area. Do not use cross-run cache, contact a network service, or write,
publish, or deploy the target skill.
