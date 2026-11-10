# Kill-path sidecar recovery cannot parse the milestone it is given

Status: known legacy defect, deferred by operator decision on 2026-10-08
(found during the call-lineage review). It does not block lineage: child ids
come from the `thread_id` milestone and the success-path result frame, never
from this path.

## Symptom

A worker outcome over 1 MiB goes to a sidecar file and is announced twice: on
stderr as a `sidecar_published` milestone, and on stdout as a reference frame.
When the stdout frame is lost (read error, or a kill before delivery) and the
dispatcher did receive the `sidecar_published` milestone, `_run_worker` tries
to recover the outcome from that milestone. The attempt always fails: it logs
`sidecar recovery failed: ValueError('RunOutcome missing parse_result')`, and
the call returns its kill error (for example `transport_failure`) instead of
the recovered outcome. Without the milestone no recovery is attempted at all.

## Root cause

The two announcements use different keys. `worker.py` emits the milestone
with `size` / `sha256` / `path` and the stdout frame with `runoutcome_ref` /
`size` / `sha256`. `_parse_runoutcome_dict` dereferences only
`runoutcome_ref`; handed the milestone, it skips the sidecar, finds no
`parse_result` and raises. Both sides were already like this in `83ce238`
(2026-05-12, per-call worker subprocess), the commit that introduced the
milestone.

Reproduced 2026-10-08 against the current code: the reference frame parses,
while the milestone, built exactly as `worker.py` emits it, raises the error
above. The codex and gemini reviews reached the same reading independently
from the source.

## Fix sketch

Make the two payloads agree (emit `runoutcome_ref` in the milestone, or map
`path` to it at the recovery site) and add the missing test: the dispatcher
receives a sidecar milestone, then loses the stdout frame, and must yield the
recovered outcome. If that outcome should also reach lineage when the
`thread_id` milestone was missed, report its session id at the recovery site
the way the success path does.
