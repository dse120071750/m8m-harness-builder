"""Closed Builder 3.1 identities for locally authored candidate handlers."""

from __future__ import annotations

import re


BUILDER_EXECUTION_ABI_VERSION = "3.1.0"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def canonical_candidate_executor_ref(flow_id: str, milestone_id: str) -> str:
    """Return the sole local candidate ref for one authored handler slot.

    The ref closes the logical flow/milestone slot.  The runtime's existing
    implementation fingerprint closes the handler path and bytes assigned to
    that slot, so neither an arbitrary release ref nor silent source drift can
    change the declared behavior identity.
    """

    if not _ID.fullmatch(str(flow_id or "")):
        raise ValueError(f"invalid flow id for candidate executor ref: {flow_id}")
    if not _ID.fullmatch(str(milestone_id or "")):
        raise ValueError(
            f"invalid milestone id for candidate executor ref: {milestone_id}"
        )
    return (
        f"handler.{flow_id}.{milestone_id}@"
        f"{BUILDER_EXECUTION_ABI_VERSION}"
    )
