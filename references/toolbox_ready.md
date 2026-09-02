# Builder Toolbox Ready

## `construct_toolbox`

Consume the chosen source audit and prepare the toolbox only in this run's staging area. Record each implementation as runnable or explicitly `BUILD_REQUIRED`, retain the ordered written-member list, and return the result through `toolbox_manifest`. Do not use cross-run cache, contact a network service, or install, publish, or deploy the target.
