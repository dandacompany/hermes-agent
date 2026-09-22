"""Versioned, board-local approval policy for opt-in Kanban tasks.

Trusted operators may approve; worker APIs can only approve a bound independent
review run. This is a lifecycle boundary, not a sandbox against host DB access.
"""
from __future__ import annotations

import json
import os

API_VERSION = 1


class ReviewPolicyError(ValueError):
    """A policy precondition failed; consumers should report a conflict."""


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS task_review_policies (
        task_id TEXT PRIMARY KEY, policy TEXT NOT NULL,
        policy_revision INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'awaiting_submission',
        review_round INTEGER NOT NULL DEFAULT 0,
        submission TEXT, approval TEXT, review_run_id INTEGER,
        require_human INTEGER NOT NULL DEFAULT 0, reason TEXT
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS task_review_approvals (
        task_id TEXT NOT NULL, request_id TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY (task_id, request_id)
    )""")
    _install_mutation_triggers(conn)


def get_review_state(conn, task_id):
    row = conn.execute("SELECT * FROM task_review_policies WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    state = {"policy": json.loads(row["policy"]), "policy_revision": row["policy_revision"],
            "submission": json.loads(row["submission"]) if row["submission"] else None,
            "approval": json.loads(row["approval"]) if row["approval"] else None,
            "state": row["state"], "review_round": row["review_round"], "reason": row["reason"]}
    if state["state"] == "submitted":
        state["reason"] = _dispatch_reason_for_state(conn, task_id, state)
    return state


def create_policy(conn, task_id, policy, assignee):
    if policy is None:
        return
    policy = validate_policy(policy, assignee)
    conn.execute("INSERT INTO task_review_policies (task_id, policy) VALUES (?, ?)",
                 (task_id, json.dumps(policy)))


def validate_policy(policy, assignee):
    from hermes_cli.kanban_db import _canonical_assignee
    if not isinstance(policy, dict) or set(policy) != {"version", "mode", "reviewer_profile"}:
        raise ReviewPolicyError("review policy requires version, mode and reviewer_profile")
    if type(policy["version"]) is not int or policy["version"] != 1 or policy["mode"] not in ("human", "agent"):
        raise ReviewPolicyError("unsupported review policy")
    reviewer = policy["reviewer_profile"]
    if policy["mode"] == "human":
        if reviewer is not None:
            raise ReviewPolicyError("human review policy cannot name a reviewer profile")
    else:
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ReviewPolicyError("agent review requires a reviewer profile")
        reviewer = _canonical_assignee(reviewer)
        if reviewer == _canonical_assignee(assignee):
            raise ReviewPolicyError("reviewer must differ from implementer")
    return {"version": 1, "mode": policy["mode"], "reviewer_profile": reviewer}


def _operator_only():
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        raise ReviewPolicyError("explicit operator approval is unavailable to a worker")


def _task(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ReviewPolicyError("task not found")
    return row


def inherited_policy(conn, policy, sources):
    if _ambient_worker_is_protected(conn) or any(source and get_review_state(conn, source) is not None for source in sources):
        if policy is not None and policy != {"version": 1, "mode": "human", "reviewer_profile": None}:
            raise ReviewPolicyError("children of protected tasks require human review")
        return {"version": 1, "mode": "human", "reviewer_profile": None}
    return policy



def _ambient_worker_is_protected(conn):
    """Board overrides must not erase the policy of the worker's source task."""
    worker_id = os.environ.get("HERMES_KANBAN_TASK")
    if not worker_id:
        return False
    import contextlib
    import sqlite3
    from pathlib import Path
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.sqlite_safe_read import connect_tracked
    from hermes_cli.kanban_db import kanban_db_path
    source_path = os.environ.get("HERMES_KANBAN_DB") or str(kanban_db_path())
    target_path = _main_db_file(conn)
    same_board = target_path and Path(source_path).resolve() == Path(target_path).resolve()
    if same_board and conn.execute("SELECT 1 FROM tasks WHERE id = ?", (worker_id,)).fetchone():
        return get_review_state(conn, worker_id) is not None
    try:
        with contextlib.closing(connect_tracked(Path(source_path).resolve().as_uri() + "?mode=ro", uri=True)) as source:
            if not source.execute("SELECT 1 FROM tasks WHERE id = ?", (worker_id,)).fetchone():
                # A switched home or imported board may no longer resolve the
                # ambient worker. Keep the child protected instead of guessing legacy.
                return True
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_review_policies'").fetchone():
                return False  # A genuinely legacy source board retains its behavior.
            return source.execute("SELECT 1 FROM task_review_policies WHERE task_id = ?", (worker_id,)).fetchone() is not None
    except sqlite3.Error as exc:
        raise ReviewPolicyError("cannot read worker source board review policy") from exc


def update_review_policy(conn, task_id, *, policy, expected_revision):
    from hermes_cli.kanban_db_connect import write_txn
    _operator_only()
    with write_txn(conn):
        state = get_review_state(conn, task_id)
        task = _task(conn, task_id)
        if state is None:
            raise ReviewPolicyError("legacy task has no review policy to update")
        if state["policy_revision"] != expected_revision:
            raise ReviewPolicyError("review policy revision conflict")
        if task["started_at"] is not None or task["status"] not in ("ready", "todo", "triage", "blocked"):
            raise ReviewPolicyError("review policy can only change before execution")
        validated = validate_policy(policy, task["assignee"])
        conn.execute("UPDATE task_review_policies SET policy = ?, policy_revision = policy_revision + 1 "
                     "WHERE task_id = ?", (json.dumps(validated), task_id))
        return get_review_state(conn, task_id)


def _invalidate(conn, task_id):
    conn.execute("UPDATE task_review_policies SET submission = NULL, approval = NULL, "
                 "review_run_id = NULL, state = 'awaiting_submission', reason = 'new_submission_required' "
                 "WHERE task_id = ?", (task_id,))


def guard_task_mutation(conn, task_id, fields):
    """Call inside the SAME write transaction as a generic mutation.

    Mapping values allow explicit reopen and no-op assignments to be recognized.
    An iterable is conservative: each named field is treated as changed.
    """
    if not conn.in_transaction:
        raise RuntimeError("review mutation guard requires a write transaction")
    state = get_review_state(conn, task_id)
    if state is None:
        return
    task = _task(conn, task_id)
    names = set(fields)
    if isinstance(fields, dict):
        names = {key for key in names if key not in task.keys() or fields[key] != task[key]}
    if "status" in names:
        target = fields.get("status") if isinstance(fields, dict) else None
        if target == "done" or target is None:
            raise ReviewPolicyError("review policy requires explicit approval; generic done is forbidden")
        if task["status"] == "done" or state["approval"] is not None:
            if target not in ("ready", "todo", "triage", "blocked"):
                if target != "archived":
                    raise ReviewPolicyError("reopen task explicitly before changing its state")
            elif names != {"status"}:
                raise ReviewPolicyError("reopen task before editing its approved result")
            else:
                _invalidate(conn, task_id)
    if "assignee" in names:
        if task["started_at"] is not None or task["status"] not in ("ready", "todo", "triage", "blocked"):
            raise ReviewPolicyError("assignee can only change before execution")
        if isinstance(fields, dict):
            validate_policy(state["policy"], fields["assignee"])
    protected = names - {"priority", "status"}
    if protected:
        if task["status"] == "done" or state["approval"] is not None:
            raise ReviewPolicyError("reopen task before editing its approved result")
        row = conn.execute("SELECT review_run_id FROM task_review_policies WHERE task_id = ?", (task_id,)).fetchone()
        if row["review_run_id"] is not None and task["current_run_id"] == row["review_run_id"]:
            raise ReviewPolicyError("reviewer cannot modify the submitted result; request changes")
        _invalidate(conn, task_id)


def _snapshot_hash(conn, task_id, run_id):
    import hashlib
    from pathlib import Path
    task = _task(conn, task_id)
    snapshot = {key: task[key] for key in ("title", "body", "result", "completion_contract")}
    run = conn.execute("SELECT summary, metadata, profile FROM task_runs WHERE id = ? AND task_id = ?",
                       (run_id, task_id)).fetchone()
    snapshot["run"] = dict(run) if run else None
    attachments = []
    for row in conn.execute("SELECT * FROM task_attachments WHERE task_id = ? ORDER BY id", (task_id,)):
        entry = dict(row)
        try:
            digest = hashlib.sha256()
            with Path(row["stored_path"]).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            entry["sha256"] = digest.hexdigest()
        except OSError as exc:
            raise ReviewPolicyError("submitted attachment is unavailable; a new submission is required") from exc
        attachments.append(entry)
    snapshot["attachments"] = attachments
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def submission_reviewer(conn, task_id, expected_run_id):
    state = get_review_state(conn, task_id)
    if state is None:
        return None
    task = _task(conn, task_id)
    row = conn.execute("SELECT review_run_id FROM task_review_policies WHERE task_id = ?", (task_id,)).fetchone()
    if row["review_run_id"] is not None and row["review_run_id"] == task["current_run_id"]:
        raise ReviewPolicyError("reviewer cannot resubmit or replace the result; request changes")
    if task["current_run_id"] is not None and expected_run_id != task["current_run_id"]:
        raise ReviewPolicyError("request_review requires current implementation run ownership")
    return state["policy"]["reviewer_profile"]


def record_submission(conn, task_id, run_id, implementer, summary):
    import uuid
    state = get_review_state(conn, task_id)
    if state is None:
        return
    if not summary or not summary.strip():
        raise ReviewPolicyError("review submission requires a nonempty result summary")
    from hermes_cli.kanban_pr_acceptance import _PR
    run = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    metadata = json.loads(run["metadata"]) if run and run["metadata"] else {}
    published = metadata.get("published_pr") if isinstance(metadata, dict) else None
    match = _PR.fullmatch(published) if isinstance(published, str) else None
    if match and _task(conn, task_id)["completion_contract"] == match[1]:
        conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (published, task_id))
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (summary, task_id))
    submission = {"id": uuid.uuid4().hex, "run_id": run_id, "policy_revision": state["policy_revision"],
                  "implementer": implementer, "hash": _snapshot_hash(conn, task_id, run_id)}
    flags = conn.execute("SELECT require_human FROM task_review_policies WHERE task_id = ?", (task_id,)).fetchone()
    same = state["policy"]["mode"] == "agent" and state["policy"]["reviewer_profile"] == implementer
    require_human = bool(flags["require_human"] or same)
    conn.execute("UPDATE task_review_policies SET submission = ?, approval = NULL, review_run_id = NULL, "
                 "require_human = ?, state = ?, reason = ? WHERE task_id = ?",
                 (json.dumps(submission), int(require_human), "human_required" if require_human else "submitted",
                  "independent_reviewer_required" if same else ("human_review_required" if require_human else None), task_id))


def review_dispatch_reason(conn, task_id):
    """Read-only eligibility: legacy has no additional gate; protected fails closed."""
    return _dispatch_reason_for_state(conn, task_id, get_review_state(conn, task_id))


def _dispatch_reason_for_state(conn, task_id, state):
    if state is None:
        return None
    if state["policy"]["mode"] == "human":
        return "human_review_required"
    if state["state"] == "human_required":
        return state["reason"] or "human_review_required"
    if state["submission"] is None:
        return "new_submission_required"
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    if not review_dispatch_enabled():
        return "review_dispatch_disabled"
    from hermes_cli.profiles import profile_exists
    reviewer = state["policy"]["reviewer_profile"]
    if not profile_exists(reviewer):
        return "reviewer_unavailable"
    if reviewer == state["submission"]["implementer"]:
        return "independent_reviewer_required"
    if _task(conn, task_id)["assignee"] != reviewer:
        return "reviewer_assignment_mismatch"
    return None


def prepare_review_claim(conn, task_id):
    state = get_review_state(conn, task_id)
    if state is None:
        return True
    reason = review_dispatch_reason(conn, task_id)
    if reason:
        conn.execute("UPDATE task_review_policies SET reason = ? WHERE task_id = ?", (reason, task_id))
        return False
    _verify_snapshot(conn, task_id, state)
    return True


def record_review_claim(conn, task_id, run_id):
    state = get_review_state(conn, task_id)
    if state is None:
        return
    # A reaped infrastructure attempt reuses this submission's logical round.
    submission = state["submission"]
    first = not submission.get("review_round")
    if first:
        submission["review_round"] = state["review_round"] + 1
    conn.execute("UPDATE task_review_policies SET state = 'reviewing', reason = NULL, review_run_id = ?, "
                 "review_round = ?, submission = ? WHERE task_id = ?",
                 (run_id, submission["review_round"], json.dumps(submission), task_id))


def _verify_snapshot(conn, task_id, state):
    sub = state["submission"]
    if sub is None or sub["policy_revision"] != state["policy_revision"]:
        raise ReviewPolicyError("stale review submission; submit the current result again")
    if _snapshot_hash(conn, task_id, sub["run_id"]) != sub["hash"]:
        raise ReviewPolicyError("review submission changed; submit the current result again")


def _verify_review_run(conn, task_id, state, expected_run_id):
    task = _task(conn, task_id)
    binding = conn.execute("SELECT review_run_id FROM task_review_policies WHERE task_id = ?", (task_id,)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (expected_run_id,)).fetchone()
    reviewer = state["policy"]["reviewer_profile"]
    if (state["policy"]["mode"] != "agent" or state["state"] != "reviewing" or expected_run_id is None
            or binding["review_run_id"] != expected_run_id or task["current_run_id"] != expected_run_id
            or task["status"] != "running" or run is None or run["ended_at"] is not None
            or run["profile"] != reviewer or task["assignee"] != reviewer):
        raise ReviewPolicyError("review policy requires request_review and the current independent review run")
    _verify_snapshot(conn, task_id, state)
    if reviewer == state["submission"]["implementer"]:
        raise ReviewPolicyError("reviewer must differ from implementer")
    from hermes_cli.profiles import profile_exists
    if not profile_exists(reviewer):
        raise ReviewPolicyError("reviewer is no longer available")
    return reviewer


def handle_changes(conn, task_id, reason, expected_run_id):
    """Return None for legacy; otherwise consume the transition atomically."""
    from hermes_cli import kanban_db as kb
    state = get_review_state(conn, task_id)
    if state is None:
        return None
    task = _task(conn, task_id)
    if expected_run_id is None:
        _operator_only()
        if task["status"] != "review" or task["current_run_id"] is not None:
            raise ReviewPolicyError("human changes require an idle review submission")
        conn.execute("UPDATE task_review_policies SET require_human = 1 WHERE task_id = ?", (task_id,))
        reviewer = "human"
    else:
        reviewer = _verify_review_run(conn, task_id, state, expected_run_id)
    if state["submission"] is not None:
        implementer = state["submission"]["implementer"]
    else:
        event = kb._latest_event(conn, task_id, "review_requested")
        implementer = kb._json_dict(kb._row_get(event, "payload")).get("implementer")
    if not implementer:
        raise ReviewPolicyError("review submission has no implementer")
    escalate = expected_run_id is not None and state["review_round"] >= 3
    new_status = "review" if escalate else kb._landing_status_after_parents(conn, task_id)
    run_id = kb._end_run(conn, task_id, outcome="changes_requested", status=new_status, summary=reason)
    conn.execute("UPDATE tasks SET status = ?, assignee = ?, claim_lock = NULL, claim_expires = NULL, "
                 "worker_pid = NULL, worker_started_at = NULL WHERE id = ?",
                 (new_status, task["assignee"] if escalate else implementer, task_id))
    if escalate:
        conn.execute("UPDATE task_review_policies SET state = 'human_required', require_human = 1, "
                     "review_run_id = NULL, reason = 'review_round_limit' WHERE task_id = ?", (task_id,))
    else:
        _invalidate(conn, task_id)
    kb._append_event(conn, task_id, "changes_requested", {"reason": reason, "implementer": implementer,
                     "reviewer": reviewer, "status": new_status}, run_id=run_id)
    return True, implementer


def _finish(conn, task_id, state, actor_kind, actor_id, request_id, summary=None, actor_name=None):
    import time
    from hermes_cli import kanban_db as kb
    if not kb._parents_satisfied(conn, task_id):
        raise ReviewPolicyError("review approval requires satisfied parent dependencies")
    sub = state["submission"]
    receipt = {"actor_kind": actor_kind, "actor_id": actor_id, "submission_id": sub["id"],
               "policy_revision": state["policy_revision"], "hash": sub["hash"],
               "approved_at": int(time.time()), "request_id": request_id}
    if actor_kind == "human" and actor_name is not None:
        receipt["actor_name"] = actor_name
    conn.execute("INSERT INTO task_review_approvals (task_id, request_id, receipt) VALUES (?, ?, ?)",
                 (task_id, request_id, json.dumps(receipt)))
    conn.execute("UPDATE task_review_policies SET approval = ?, state = 'approved', reason = NULL "
                 "WHERE task_id = ?", (json.dumps(receipt), task_id))
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ?, claim_lock = NULL, claim_expires = NULL, "
                 "worker_pid = NULL, block_kind = NULL, block_recurrences = 0 WHERE id = ?",
                 (receipt["approved_at"], task_id))
    run_id = kb._end_run(conn, task_id, outcome="completed", status="done", summary=summary)
    kb._append_event(conn, task_id, "completed", {"approval": receipt, "summary": summary}, run_id=run_id)
    return run_id


def _after_complete(conn, task_id, run_id, summary, fire_lifecycle_hook=True):
    from hermes_cli import kanban_db as kb
    kb._clear_failure_counter(conn, task_id)
    kb.recompute_ready(conn)
    kb._cleanup_workspace(conn, task_id)
    if fire_lifecycle_hook:
        kb._fire_task_hook("kanban_task_completed", kb.get_task(conn, task_id), task_id, run_id, summary=summary)


def approve_task(conn, task_id, *, actor_id, submission_id, request_id, actor_name=None):
    from hermes_cli.kanban_db_connect import write_txn
    _operator_only()
    if any(not isinstance(value, str) or not value.strip() for value in (actor_id, submission_id, request_id)):
        raise ReviewPolicyError("operator actor_id, submission_id and request_id are required")
    if actor_name is not None:
        if not isinstance(actor_name, str) or not actor_name.strip() or len(actor_name) > 200:
            raise ReviewPolicyError("operator actor_name must be nonempty text of at most 200 characters")
        actor_name = actor_name.strip()
    acceptance = _prepare_review_acceptance(conn, task_id, None)
    with write_txn(conn):
        state = get_review_state(conn, task_id)
        if state is None:
            raise ReviewPolicyError("task has no review policy")
        old = conn.execute("SELECT receipt FROM task_review_approvals WHERE task_id = ? AND request_id = ?",
                           (task_id, request_id)).fetchone()
        if old:
            receipt = json.loads(old["receipt"])
            if (receipt["actor_kind"], receipt["actor_id"], receipt["submission_id"]) != ("human", actor_id, submission_id):
                raise ReviewPolicyError("approval request id conflict")
            if state["approval"] != receipt:
                raise ReviewPolicyError("approval belongs to an older submission")
            return state
        task = _task(conn, task_id)
        if task["status"] != "review" or task["current_run_id"] is not None or task["claim_lock"] is not None:
            raise ReviewPolicyError("operator approval requires an idle review submission; reclaim active review first")
        _verify_snapshot(conn, task_id, state)
        if state["submission"]["id"] != submission_id:
            raise ReviewPolicyError("stale review submission")
        _record_review_acceptance(conn, task_id, acceptance)
        run_id = _finish(conn, task_id, state, "human", actor_id, request_id, actor_name=actor_name)
    _after_complete(conn, task_id, run_id, None)
    return get_review_state(conn, task_id)


def complete_review(conn, task_id, *, expected_run_id=None, result=None, summary=None,
                    metadata=None, force=False, fire_lifecycle_hook=True):
    from hermes_cli.kanban_db_connect import write_txn
    acceptance = _prepare_review_acceptance(conn, task_id, expected_run_id)
    with write_txn(conn):
        state = get_review_state(conn, task_id)
        receipt = state["approval"]
        if (receipt is not None and expected_run_id is not None
                and receipt["actor_kind"] == "agent"
                and receipt["request_id"] == f"review-run:{expected_run_id}"
                and receipt["submission_id"] == state["submission"]["id"]):
            return True
        reviewer = _verify_review_run(conn, task_id, state, expected_run_id)
        if isinstance(metadata, dict) and metadata.get("artifacts"):
            raise ReviewPolicyError("reviewer cannot replace attachments; request changes")
        summary = summary if summary is not None else result
        _record_review_acceptance(conn, task_id, acceptance)
        run_id = _finish(conn, task_id, state, "agent", reviewer, f"review-run:{expected_run_id}", summary)
    _after_complete(conn, task_id, run_id, summary, fire_lifecycle_hook)
    return True


def _install_mutation_triggers(conn):
    """Fence raw adapters as well as native functions; never infer approval from done."""
    conn.execute("""CREATE TRIGGER IF NOT EXISTS review_policy_done_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'done' AND OLD.status != 'done'
         AND EXISTS (SELECT 1 FROM task_review_policies WHERE task_id = NEW.id
                     AND (state != 'approved' OR approval IS NULL OR submission IS NULL))
        BEGIN SELECT RAISE(ABORT, 'review policy requires explicit approval'); END""")
    condition = "OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body OR OLD.result IS NOT NEW.result OR OLD.completion_contract IS NOT NEW.completion_contract"
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS review_policy_reviewer_edit_guard
        BEFORE UPDATE OF title, body, result, completion_contract ON tasks
        WHEN ({condition}) AND EXISTS (SELECT 1 FROM task_review_policies
          WHERE task_id = OLD.id AND review_run_id IS NOT NULL AND review_run_id = OLD.current_run_id)
        BEGIN SELECT RAISE(ABORT, 'reviewer cannot modify submitted results'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS review_policy_done_edit_guard
        BEFORE UPDATE OF title, body, result, completion_contract ON tasks
        WHEN (OLD.status = 'done' OR EXISTS (SELECT 1 FROM task_review_policies WHERE task_id = OLD.id AND approval IS NOT NULL)) AND ({condition})
         AND EXISTS (SELECT 1 FROM task_review_policies WHERE task_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'reopen task before editing approved result'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS review_policy_condition_changed
        AFTER UPDATE OF title, body, result, completion_contract ON tasks WHEN {condition}
        BEGIN UPDATE task_review_policies SET submission = NULL, approval = NULL,
              state = 'awaiting_submission', review_run_id = NULL, reason = 'new_submission_required'
              WHERE task_id = NEW.id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS review_policy_reopened
        AFTER UPDATE OF status ON tasks
        WHEN OLD.status IN ('done', 'archived') AND NEW.status NOT IN ('done', 'archived')
        BEGIN UPDATE task_review_policies SET submission = NULL, approval = NULL,
              state = 'awaiting_submission', review_run_id = NULL, reason = 'new_submission_required'
              WHERE task_id = NEW.id; END""")
    for action, source in (("INSERT", "NEW"), ("DELETE", "OLD"), ("UPDATE", "OLD")):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS review_attachment_{action.lower()}_guard
            BEFORE {action} ON task_attachments
            WHEN EXISTS (SELECT 1 FROM task_review_policies p JOIN tasks t ON t.id = p.task_id
              WHERE p.task_id = {source}.task_id AND (t.status = 'done' OR p.approval IS NOT NULL OR
                 (t.current_run_id IS NOT NULL AND t.current_run_id = p.review_run_id)))
            BEGIN SELECT RAISE(ABORT, 'review policy forbids result attachment mutation'); END""")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS review_attachment_{action.lower()}_invalidate
            AFTER {action} ON task_attachments
            BEGIN UPDATE task_review_policies SET submission = NULL, approval = NULL,
              state = 'awaiting_submission', review_run_id = NULL, reason = 'new_submission_required'
              WHERE task_id = {source}.task_id; END""")


def patch_review_task(conn, task_id, *, fields, policy=None, expected_revision=None):
    """Atomic protected-field/policy edit; status transitions use explicit APIs."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    allowed = {"title", "body", "priority", "assignee", "model_override", "provider_override", "reasoning_effort"}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ReviewPolicyError("unsupported review task fields; status requires a separate transition")
    fields = dict(fields)
    if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip()):
        raise ReviewPolicyError("title is required")
    if "title" in fields:
        fields["title"] = fields["title"].strip()
    if "body" in fields and fields["body"] is not None and not isinstance(fields["body"], str):
        raise ReviewPolicyError("body must be text or null")
    if "priority" in fields and type(fields["priority"]) is not int:
        raise ReviewPolicyError("priority must be an integer")
    if "assignee" in fields:
        fields["assignee"] = kb._canonical_assignee(fields["assignee"])
    if "reasoning_effort" in fields:
        fields["reasoning_effort"] = kb.normalize_reasoning_effort(fields["reasoning_effort"])
    with write_txn(conn):
        state = get_review_state(conn, task_id)
        task = _task(conn, task_id)
        if state is None:
            raise ReviewPolicyError("task has no review policy")
        if {"model_override", "provider_override"} & set(fields):
            model, provider = kb._validate_model_override(fields.get("model_override", task["model_override"]),
                                                         fields.get("provider_override", task["provider_override"]))
            fields.update(model_override=model, provider_override=provider)
        if policy is not None:
            _operator_only()
            if state["policy_revision"] != expected_revision:
                raise ReviewPolicyError("review policy revision conflict")
            validated = validate_policy(policy, fields.get("assignee", task["assignee"]))
            if validated != state["policy"]:
                if task["started_at"] is not None or task["status"] not in ("ready", "todo", "triage", "blocked"):
                    raise ReviewPolicyError("review policy can only change before execution")
                conn.execute("UPDATE task_review_policies SET policy = ?, policy_revision = policy_revision + 1 "
                             "WHERE task_id = ?", (json.dumps(validated), task_id))
        guard_task_mutation(conn, task_id, fields)
        if fields:
            conn.execute("UPDATE tasks SET " + ", ".join(f"{name} = ?" for name in fields) + " WHERE id = ?",
                         (*fields.values(), task_id))
        kb._append_event(conn, task_id, "edited", {"fields": list(fields), "review_policy": policy is not None})
    kb.notify_task_updated(conn, task_id, list(fields) + (["review_policy"] if policy is not None else []))
    return get_review_state(conn, task_id)


def escalate_review(conn, task_id, expected_run_id, reason):
    """A reviewer's needs-input verdict preserves evidence for a human."""
    from hermes_cli import kanban_db as kb
    state = get_review_state(conn, task_id)
    if state is None or state["state"] != "reviewing":
        return False
    _verify_review_run(conn, task_id, state, expected_run_id)
    run_id = kb._end_run(conn, task_id, outcome="blocked", status="review", summary=reason)
    conn.execute("UPDATE tasks SET status = 'review', claim_lock = NULL, claim_expires = NULL, "
                 "worker_pid = NULL, worker_started_at = NULL WHERE id = ?", (task_id,))
    conn.execute("UPDATE task_review_policies SET state = 'human_required', require_human = 1, "
                 "review_run_id = NULL, reason = 'reviewer_needs_input' WHERE task_id = ?", (task_id,))
    kb._append_event(conn, task_id, "review_escalated", {"reason": reason}, run_id=run_id)
    return True


def _prepare_review_acceptance(conn, task_id, expected_run_id):
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance
    state = get_review_state(conn, task_id)
    if state is None or state["submission"] is None or _task(conn, task_id)["status"] == "done":
        return None
    run = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (state["submission"]["run_id"],)).fetchone()
    metadata = json.loads(run["metadata"]) if run and run["metadata"] else None
    return prepare_acceptance(conn, task_id, expected_run_id, metadata)


def _record_review_acceptance(conn, task_id, acceptance):
    from hermes_cli.kanban_pr_acceptance_store import record_acceptance
    if acceptance is False or (acceptance is not None and not record_acceptance(conn, task_id, acceptance)):
        raise ReviewPolicyError("completion acceptance contract is not satisfied")
