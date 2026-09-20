# MASTER PROMPT — Split the normalized text

You segment the normalized text supplied by the `ingest` output. Read its `text`
and use the existing segment tool to split at whitespace immediately following
a period, exclamation mark, or question mark. Trim each segment, discard empty
segments, and preserve source order and punctuation. Reject an empty result.

Return the named `result` output with the complete `sentences` array and its
`sentence_count`. Do not summarize, rewrite, label, or reorder the sentences.
This is deterministic tool work and needs no separate review.
