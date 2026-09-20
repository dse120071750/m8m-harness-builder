# MASTER PROMPT — Normalize the source text

You prepare text for the next milestone. Read `request.text` from the bound user
request and normalize whitespace: replace each run of whitespace with one space
and trim its leading and trailing spaces. Preserve the wording, punctuation,
and order. Reject text that is empty after normalization.

Use the existing ingest tool. Return the named `result` output with `text` and
its `char_count`. The output must contain the actual normalized text, not a
description of what was done. No model call or semantic review is needed.
