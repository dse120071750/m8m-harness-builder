# Builder Skill Shipped

## `install_local_skill`

Consume the exact chosen `workflow_source_bundle`, its linked `staged_harness` report, and the bound PASS validation report. Verify the report's bundle path and digest against the portable bundle, revalidate the exact staged member manifest, then copy only those declared members into the requested local destinations and return `installation_receipt`. This FlowStep is a bounded local copy: do not use cross-run cache, contact a network service, push, publish, deploy, or activate anything.
