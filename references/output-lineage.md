# Output assets and session lineage

Use this guidance when a workflow produces media/files or needs server-visible
saved results. Keep the complete master prompt and structured output contracts.
Lineage is host metadata, not another milestone, input schema, judge, or model
task. Do not add fields to the closed `m8m_chosen_output_v1` or
`flowstep_output_v3` envelopes.

## Author the actual delivery

- Declare generated files/images/video/audio as typed output ports. Many-member
  outputs return stable local `id`, nonempty `name`, and actual produced `path`
  in delivery order. Write only in runtime-supplied work storage; the runtime
  freezes the files. Prose saying generation succeeded is not a delivered asset.
- JSON/data is also an addressable output. Return the full structured value,
  even when the frontend displays a bounded preview. Bind later work to the
  chosen named output, never to a transcript or another run's files.
- Keep existing provider/content-pool IDs in their domain contracts. Selecting
  or copying an image is reuse, not generation. A JSON-only legacy tool needs an
  explicit adapter for its known asset fields or an intentional typed-port edit;
  do not scan arbitrary JSON strings for URLs and call them images.

## Identity ownership

The server retains `execution_id` as the workflow-session ID and `room_id` as the
chat-session ID. It assigns deterministic milestone, output-record, and member
asset IDs from the registered run/milestone/attempt/port/member keys. Prompts and
tools must not mint these global IDs or claim completion. Local port/member IDs
remain unchanged. Identical bytes in separate runs still have separate output
occurrences; byte equality does not establish provenance.

An originating chat is recorded by admission, not supplied by a milestone.
Attaching a saved run to another room does not change its origin. Direct Run,
Time Machine, and local coordination do not require a fake chat. A parent-chat
reference does not grant implicit access to chat history.

## Optional trusted producer metadata

Only use this extension when the target host implements and enables it. Older
installed runtimes remain pinned; editing this skill does not upgrade a server.
The native host assigns unknown provenance when trusted evidence is absent.

An in-process native producer may call its provided
`context.record_output_provenance(members)` during the actual invocation. An
installed Python-function adapter may instead write
`m8m-output-provenance.json` in its supplied `work_dir`, with this closed shape:

```json
{
  "schema": "m8m.output_provenance.v1",
  "members": [
    {"output_id":"images","member_id":"img1","provenance_kind":"generated","sources":[]},
    {"output_id":"images","member_id":"img2","provenance_kind":"reused","sources":[{"input":"source_images","member_id":"img2"}]},
    {"output_id":"summary","member_id":"summary","provenance_kind":"derived","sources":[{"input":"facts","member_id":"facts"}]}
  ]
}
```

The members must exist in that invocation's real return. The sidecar is private
adapter metadata, not a business output, chosen manifest, or acceptance receipt.
Generated has no sources; reused has exactly one; derived has at least one.
An upstream source names an actual bound input alias and its chosen member ID.
An external source instead uses `{"owner":"nisan-n8n","asset_id":"stable-id"}`
with the actual owner's ID, never an expiring URL, absolute path, credential, or
invented M8M asset ID. The host binds the producing tool and resolves upstream
identity against frozen inputs and tenant scope. A model cannot claim provenance
by adding these fields to candidate JSON.

## Verify without changing ownership

For a new media adapter, exercise actual ordered files plus structured JSON and
a downstream chosen-output consumer. Verify retrievable bytes, member order,
generated versus reused/derived evidence, and complete large-value retrieval on
the target host. Do not present transport-only or synthetic-manifest tests as
full native execution. Existing working adapters need only relevant regression.

Local coordination still uses its codebase-owned runner and filesystem authority.
Installing a definition does not import earlier local runs; any later run import
needs an explicit source-run mapping. Packaging and server installation remain
separately requested actions. Never rebuild deployed product packages solely to
attach host-owned IDs, or imply that a local skill edit changes admitted runs.
