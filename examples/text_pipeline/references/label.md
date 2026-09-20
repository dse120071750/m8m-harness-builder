# MASTER PROMPT — Classify the first sentence

You classify the first sentence from the bound `segment.sentences` array. Use
only that sentence and choose exactly one label: `question`, `statement`, or
`other`. Choose `question` when it asks a question, `statement` when it makes
a statement, and `other` when neither category fits. Do not classify later
sentences or infer missing context from previous conversations.

When the label tool requests a draft, return only a JSON object containing
`label` with one of those three values. The existing tool validates the label
and returns the named `result` output containing the label and original
sentence. Do not return an explanation, a checksum, or a review receipt.
