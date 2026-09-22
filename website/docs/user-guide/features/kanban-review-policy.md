---
sidebar_position: 19
---

# Per-task approval policies

Native callers can opt a task into human or independent-agent review without
changing a board's existing tasks or its global `kanban.review_dispatch` setting.
Omitting the policy preserves the legacy lifecycle. The caller chooses whether
new tasks should default to human review.

```python
from hermes_cli import kanban_db
from hermes_cli.kanban_review_policy import get_review_state, approve_task

policy = {"version": 1, "mode": "human", "reviewer_profile": None}
task_id = kanban_db.create_task(conn, title="Draft announcement", review_policy=policy)
```

Agent review uses `{"version": 1, "mode": "agent", "reviewer_profile": "reviewer"}`.
Profile identifiers are normalized and the reviewer must differ from the
implementer. A missing reviewer or disabled global review dispatch leaves the
submission waiting, with a reason in its review state. No substitute is selected.
Children inherit human review, including decomposition and creation on another
board by a protected worker. An unresolved ambient worker origin conservatively
requires human review; an unreadable explicit source database is refused.

Implementation workers submit with `request_review`; ordinary `complete`, force,
status PATCH and dashboard drag cannot approve their result. Submission captures
the implementation run, policy revision, result and conditions, and content hashes
of preserved attachments. Only a claimed independent review run can approve with
`complete_task`. Review commentary does not replace the submitted result. Reviewers
request revisions with `request_changes`, or use a `needs_input` block to escalate
to a human. Three unsuccessful review rounds stop automatic review. Infrastructure
reclaims of the same submission do not consume another logical round.

An operator approves the current submission explicitly:

```python
state = get_review_state(conn, task_id)
approve_task(conn, task_id, actor_id="application:user-123",
             submission_id=state["submission"]["id"], request_id="unique-click-id")
```

The caller must authenticate the operator and construct `actor_id` itself. Worker
and delegated-child process contexts cannot use the operator API. A live review
run must finish or be reclaimed before human approval. The local CLI equivalent is
`hermes kanban approve TASK --submission-id ID --request-id ID`; its actor is the
local OS account. This uses Hermes's trusted-local model, not cryptographic proof
of a human click or an OS boundary against arbitrary file/DB access.

Approval receipt and completion commit atomically. Repeated identical requests
return the original success without another event. Old submissions conflict.
Changing conditions or attachments invalidates the current submission and requires
resubmission; an operator can reopen even an already-invalidated submission.
Approved results, including archived approved results, require explicit reopening
before edits. Reopening retains approval history but removes its current effect.
An operator's revision request keeps subsequent submissions waiting for a human.
Policy and assignee changes are allowed only before execution; resubmitting
unchanged editor values is a no-op.

## Native integration contract

`hermes_cli.kanban_review_policy.API_VERSION == 1` identifies this persisted
capability. Adapters should require the complete API before exposing protected
creation, and otherwise preserve legacy reads while refusing protected writes.

- `get_review_state(conn, task_id)` returns `None` for legacy tasks, otherwise
  `policy`, `policy_revision`, `submission`, `review_round`, `state`, `approval`,
  and `reason`. States are `awaiting_submission`, `submitted`, `reviewing`,
  `human_required`, and `approved`.
- `approve_task(conn, task_id, *, actor_id, submission_id, request_id, actor_name=None)` returns
  the review state after approval or an identical retry. Optional authenticated
  `actor_name` is bounded to 200 characters and preserved in the human receipt;
  stable `actor_id` determines identity and idempotence.
- `update_review_policy(conn, task_id, *, policy, expected_revision)` uses an
  optimistic revision check and returns the updated review state.
- `patch_review_task(conn, task_id, *, fields, policy=None, expected_revision=None)`
  atomically edits title, body, priority, assignee, model/provider override, and
  reasoning effort, optionally alongside policy. Status transitions are separate.
- `guard_task_mutation(conn, task_id, fields)` protects an adapter's raw mutation.
  It must run inside the same `write_txn` as that mutation. Pass a mapping of
  proposed field values to recognize no-op writes. Do not wrap native transition
  functions in an outer transaction: their notifications happen after commit.
- `ReviewPolicyError` subclasses `ValueError`; adapters should expose it as a
  conflict with the actionable message, without silently weakening the policy.

The additive `task_review_policies` and `task_review_approvals` tables belong to
the board database. SQLite triggers also fence raw completion and attachment
writers. Existing PR completion contracts remain required in addition to review.
No policy is copied into an external application's database.

## Rollback

Once a board contains protected tasks, keep this policy-aware core installed.
Rollback the caller UI or disable new protected creation first. An older binary
cannot reliably recognize a newer policy requirement; persisted SQL guards are
not a supported binary downgrade strategy. A core downgrade requires a stopped
dispatcher and an explicit board backup/restore plan. Do not remove policy tables
or approval history to make an old binary run.
