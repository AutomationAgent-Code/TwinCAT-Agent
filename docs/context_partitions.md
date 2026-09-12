# Request context partitions

Ordinary tool projection allows 12,000 characters; preflight and compiler/HMI
diagnostic projections allow 32,000. PLC source retains its own contiguous-page
contract. These are character budgets, not token counts or provider window limits.

Before foreground/worker requests, `trim_to_budget` builds a non-mutating view:

* Hot evidence: latest two tool results keep their existing detailed projections.
* Working evidence: older large results in the current turn retain decision
  evidence and source identity/hash, not repeated source or log bodies.
* Historical evidence: older-turn large results use the same compact envelope.
* Protected evidence: top-level build plans, document/member baselines are retained.

System instructions, project memory, durable ledger and authorization gates remain
with their existing owners. This is not a second authority or semantic retrieval
engine. User messages, assistant tool arguments, call IDs and order are unchanged.
Raw persisted results are not deleted. Compaction does not imply a successful
review, current source baseline, or a renewed authorization.

The existing 60,000-character conversation budget is still soft: protected data,
a large current user request or assistant tool arguments can exceed it. This
implementation does not claim hard token accounting across system prompts, tool
schemas, images and every provider. Older bodies are not automatically rehydrated;
read current source through normal scoped tools when a new baseline is needed.
