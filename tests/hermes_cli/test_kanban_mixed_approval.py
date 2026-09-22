"""Native per-task review policy, exercised against real isolated board databases."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}
AGENT = {"version": 1, "mode": "agent", "reviewer_profile": "reviewer"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect() as db:
        yield db


def test_human_policy_worker_cannot_complete(conn):
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN)
    run = kb.claim_task(conn, tid)
    with pytest.raises(ValueError, match="review"):
        kb.complete_task(conn, tid, summary="done", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).status != "done"


def submit(conn, policy=HUMAN):
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=policy)
    run = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="deliverable", expected_run_id=run.current_run_id)
    return tid


def test_human_submission_never_dispatches_and_operator_approval_is_idempotent(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    assert kb.claim_review_task(conn, tid) is None
    state = rp.get_review_state(conn, tid)
    assert state["state"] == "submitted"
    assert state["submission"]["hash"]
    args = dict(actor_id="user:1", submission_id=state["submission"]["id"], request_id="click-1")
    approved = rp.approve_task(conn, tid, **args)
    assert approved["approval"]["actor_kind"] == "human"
    assert kb.get_task(conn, tid).status == "done"
    count = len(kb.list_events(conn, tid))
    assert rp.approve_task(conn, tid, **args) == approved
    assert len(kb.list_events(conn, tid)) == count


def test_agent_claim_binds_submission_and_approves_preserved_result(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "reviewer")
    tid = submit(conn, AGENT)
    state = rp.get_review_state(conn, tid)
    run = kb.claim_review_task(conn, tid)
    assert run.assignee == "reviewer"
    assert rp.get_review_state(conn, tid)["review_round"] == 1
    with pytest.raises(ValueError):
        rp.approve_task(conn, tid, actor_id="user", submission_id=state["submission"]["id"], request_id="busy")
    assert kb.complete_task(conn, tid, summary="review passed", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).result == "deliverable"
    assert rp.get_review_state(conn, tid)["approval"]["actor_kind"] == "agent"


def test_third_unsuccessful_review_requires_human(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    for n in range(3):
        run = kb.claim_review_task(conn, tid)
        assert run is not None
        assert kb.request_changes(conn, tid, reason="revise", expected_run_id=run.current_run_id)[0]
        if n < 2:
            worker = kb.claim_task(conn, tid)
            assert kb.request_review(conn, tid, summary=f"revision {n}", expected_run_id=worker.current_run_id)
    assert kb.get_task(conn, tid).status == "review"
    assert rp.get_review_state(conn, tid)["state"] == "human_required"
    assert rp.get_review_state(conn, tid)["review_round"] == 3
    assert kb.claim_review_task(conn, tid) is None


def test_worker_cannot_assert_operator_identity(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    state = rp.get_review_state(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(ValueError, match="operator"):
        rp.approve_task(conn, tid, actor_id="user", submission_id=state["submission"]["id"], request_id="forged")


def test_mutations_invalidate_submission_and_done_edits_are_refused(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    old = rp.get_review_state(conn, tid)["submission"]["id"]
    assert kb.edit_task(conn, tid, body="new conditions")
    with pytest.raises(ValueError):
        rp.approve_task(conn, tid, actor_id="user", submission_id=old, request_id="stale")
    assert rp.get_review_state(conn, tid)["submission"] is None


def test_child_policy_inheritance_and_policy_revision(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = kb.create_task(conn, title="parent", assignee="writer", review_policy=HUMAN)
    child = kb.create_task(conn, title="child", creator_task_id=tid, review_policy=None)
    assert rp.get_review_state(conn, child)["policy"] == HUMAN
    changed = rp.update_review_policy(conn, tid, policy=AGENT, expected_revision=1)
    assert changed["policy_revision"] == 2
    with pytest.raises(ValueError):
        rp.update_review_policy(conn, tid, policy=HUMAN, expected_revision=1)
    kb.claim_task(conn, tid)
    with pytest.raises(ValueError):
        rp.update_review_policy(conn, tid, policy=HUMAN, expected_revision=2)


def test_protected_patch_is_atomic_and_can_change_policy_with_assignee(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=AGENT)
    state = rp.patch_review_task(conn, tid, fields={"title": "new", "assignee": "reviewer"},
                                policy=HUMAN, expected_revision=1)
    assert state["policy"] == HUMAN
    assert kb.get_task(conn, tid).assignee == "reviewer"
    kb.claim_task(conn, tid)
    with pytest.raises(ValueError):
        rp.patch_review_task(conn, tid, fields={"title": "bad", "assignee": "other"})
    assert kb.get_task(conn, tid).title == "new"


def test_dispatch_lane_does_not_consume_human_tasks(conn):
    from hermes_cli import kanban_db_dispatch as dispatch
    tid = submit(conn)
    assert all(row["id"] != tid for row in dispatch._lane_rows(conn, "review"))
    assert not dispatch.has_spawnable_review(conn)


def test_raw_done_and_reviewer_mutation_are_fenced(conn, monkeypatch):
    import sqlite3
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    with pytest.raises(sqlite3.IntegrityError, match="review"):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    kb.claim_review_task(conn, tid)
    with pytest.raises(sqlite3.IntegrityError, match="review"):
        conn.execute("UPDATE tasks SET result = 'tampered' WHERE id = ?", (tid,))


def test_missing_profile_and_global_pause_never_fallback(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    from hermes_cli import kanban_db_dispatch as dispatch
    tid = submit(conn, AGENT)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    assert kb.claim_review_task(conn, tid) is None
    assert rp.get_review_state(conn, tid)["reason"] == "reviewer_unavailable"
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: False)
    assert kb.claim_review_task(conn, tid) is None
    assert rp.get_review_state(conn, tid)["reason"] == "review_dispatch_disabled"


def test_human_revision_stays_human_after_agent_escalation(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    assert kb.request_changes(conn, tid, reason="operator revision")[0]
    run = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="new", expected_run_id=run.current_run_id)
    assert rp.get_review_state(conn, tid)["state"] == "human_required"
    assert kb.claim_review_task(conn, tid) is None


def test_stale_claim_and_worker_reviewer_override_cannot_approve(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=AGENT)
    worker = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="result", reviewer="writer", expected_run_id=worker.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert review.assignee == "reviewer"
    with pytest.raises(ValueError):
        kb.complete_task(conn, tid, summary="forged", expected_run_id=worker.current_run_id, force=True)
    with pytest.raises(ValueError):
        kb.complete_task(conn, tid, summary="forged", force=True)
    assert rp.get_review_state(conn, tid)["state"] == "reviewing"


def test_completed_attachments_and_content_immutable_until_reopen(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    sub = rp.get_review_state(conn, tid)["submission"]
    rp.approve_task(conn, tid, actor_id="user", submission_id=sub["id"], request_id="one")
    with pytest.raises(ValueError):
        kb.edit_task(conn, tid, title="tamper")
    with pytest.raises(ValueError):
        kb.store_attachment_bytes(conn, tid, "x.txt", b"tamper")
    with kbc.write_txn(conn):
        rp.guard_task_mutation(conn, tid, {"status": "ready"})
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert rp.get_review_state(conn, tid)["approval"] is None
    assert conn.execute("SELECT COUNT(*) FROM task_review_approvals WHERE task_id = ?", (tid,)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        rp.approve_task(conn, tid, actor_id="user", submission_id=sub["id"], request_id="one")


def test_attachment_bytes_hash_and_metadata_changes_invalidate_approval(conn, tmp_path):
    from hermes_cli import kanban_review_policy as rp
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN)
    attachment = tmp_path / "result.txt"
    attachment.write_text("before")
    kb.add_attachment(conn, tid, filename="result.txt", stored_path=str(attachment))
    worker = kb.claim_task(conn, tid)
    kb.request_review(conn, tid, summary="result", expected_run_id=worker.current_run_id)
    sub = rp.get_review_state(conn, tid)["submission"]
    attachment.write_text("after")
    with pytest.raises(ValueError, match="changed"):
        rp.approve_task(conn, tid, actor_id="user", submission_id=sub["id"], request_id="stale")
    assert kb.get_task(conn, tid).status == "review"


def test_decomposition_inherits_human_policy(conn):
    from hermes_cli import kanban_review_policy as rp
    from hermes_cli.kanban_db_graph import decompose_triage_task
    tid = kb.create_task(conn, title="plan", triage=True, review_policy=HUMAN)
    children = decompose_triage_task(conn, tid, root_assignee="writer", children=[{"title": "part", "assignee": "writer"}])
    assert children
    assert rp.get_review_state(conn, children[0])["policy"] == HUMAN


def test_native_reopen_review_invalidates_and_requires_human(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn, AGENT)
    assert kb.reopen_review_task(conn, tid)
    assert rp.get_review_state(conn, tid)["submission"] is None
    run = kb.claim_task(conn, tid)
    kb.request_review(conn, tid, summary="fixed", expected_run_id=run.current_run_id)
    assert rp.get_review_state(conn, tid)["state"] == "human_required"


def test_agent_infrastructure_retry_does_not_increment_round(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    old = kb.claim_review_task(conn, tid)
    assert kb.reclaim_task(conn, tid)
    new = kb.claim_review_task(conn, tid)
    assert new.current_run_id != old.current_run_id
    assert rp.get_review_state(conn, tid)["review_round"] == 1
    with pytest.raises(ValueError):
        kb.complete_task(conn, tid, summary="late", expected_run_id=old.current_run_id)
    assert kb.complete_task(conn, tid, summary="good", expected_run_id=new.current_run_id)


def test_third_round_can_succeed(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    for n in range(2):
        review = kb.claim_review_task(conn, tid)
        kb.request_changes(conn, tid, reason="revise", expected_run_id=review.current_run_id)
        impl = kb.claim_task(conn, tid)
        kb.request_review(conn, tid, summary=f"fixed {n}", expected_run_id=impl.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="pass", expected_run_id=review.current_run_id)
    assert rp.get_review_state(conn, tid)["review_round"] == 3


def test_legacy_and_policy_survive_reconnect(conn, tmp_path):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    legacy = kb.create_task(conn, title="legacy")
    assert rp.get_review_state(conn, legacy) is None
    assert kb.complete_task(conn, legacy, result="legacy done")
    path = kbc._main_db_file(conn)
    kbc.init_db(Path(path))
    with kbc.connect(Path(path)) as other:
        assert rp.get_review_state(other, tid) == rp.get_review_state(conn, tid)
        assert kb.claim_review_task(other, tid) is None


def test_concurrent_approval_serializes_to_one_receipt(conn):
    from concurrent.futures import ThreadPoolExecutor
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    sub = rp.get_review_state(conn, tid)["submission"]["id"]
    path = Path(kbc._main_db_file(conn))
    def approve():
        with kbc.connect(path) as other:
            return rp.approve_task(other, tid, actor_id="user", submission_id=sub, request_id="same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    assert results[0] == results[1]
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "completed"]) == 1


def test_operator_cli_approval_and_worker_denial(conn, monkeypatch):
    from argparse import Namespace
    from hermes_cli import kanban as cli, kanban_review_policy as rp
    tid = submit(conn)
    sub = rp.get_review_state(conn, tid)["submission"]["id"]
    args = Namespace(task_id=tid, submission_id=sub, request_id="cli")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert cli._cmd_approve(args) != 0
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert cli._cmd_approve(args) == 0
    assert kb.get_task(conn, tid).status == "done"


def test_agent_can_escalate_needs_input_without_retrying(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    review = kb.claim_review_task(conn, tid)
    assert kb.block_task(conn, tid, reason="human judgement required", kind="needs_input", expected_run_id=review.current_run_id)
    state = rp.get_review_state(conn, tid)
    assert state["state"] == "human_required"
    assert kb.get_task(conn, tid).status == "review"
    assert kb.claim_review_task(conn, tid) is None


def test_existing_completion_contract_is_not_bypassed(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    from hermes_cli import kanban_pr_acceptance_store as acceptance
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN,
                         completion_contract="example/repo")
    run = kb.claim_task(conn, tid)
    kb.request_review(conn, tid, summary="result", expected_run_id=run.current_run_id)
    sub = rp.get_review_state(conn, tid)["submission"]["id"]
    monkeypatch.setattr(acceptance, "collect_acceptance", lambda contract, published: {
        "ok": False, "classification": "not_merged", "detail": "pending", "recovery": "merge first"})
    with pytest.raises(ValueError, match="acceptance"):
        rp.approve_task(conn, tid, actor_id="user", submission_id=sub, request_id="no-pr")
    assert kb.get_task(conn, tid).status == "review"


def test_tools_protected_lifecycle_uses_stored_reviewer_and_session_metadata(conn, monkeypatch):
    import json
    from hermes_cli import profiles, kanban_review_policy as rp
    from tools import kanban_tools as tools
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "reviewer")
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=AGENT)
    run = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-session")
    denied = json.loads(tools._handle_complete({"summary": "done"}))
    assert denied.get("error")
    submitted = json.loads(tools._handle_request_review({"summary": "result", "reviewer": "nonexistent"}))
    assert submitted.get("ok") is True
    review = kb.claim_review_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    accepted = json.loads(tools._handle_complete({"summary": "passed"}))
    assert accepted.get("ok") is True
    assert rp.get_review_state(conn, tid)["approval"]["actor_kind"] == "agent"


def test_idempotent_creation_cannot_return_unprotected_task(conn):
    kb.create_task(conn, title="legacy", idempotency_key="same")
    with pytest.raises(ValueError, match="policy"):
        kb.create_task(conn, title="protected", idempotency_key="same", review_policy=HUMAN)


def test_profile_aliases_cannot_self_review(conn):
    with pytest.raises(ValueError, match="differ"):
        kb.create_task(conn, title="self", assignee="WRITER", review_policy={**AGENT, "reviewer_profile": " Writer "})


def test_noop_policy_and_assignee_edit_after_start(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN)
    kb.claim_task(conn, tid)
    rp.patch_review_task(conn, tid, fields={"body": "clearer", "assignee": "writer"}, policy=HUMAN, expected_revision=1)
    assert kb.get_task(conn, tid).body == "clearer"
    assert rp.get_review_state(conn, tid)["policy_revision"] == 1


def test_dashboard_protected_patch_is_atomic_and_rejects_done(conn):
    import importlib.util
    import sys
    from fastapi import HTTPException
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("mixed_policy_dashboard", path)
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    tid = submit(conn)
    with pytest.raises(HTTPException) as rejected:
        api.update_task(tid, api.UpdateTaskBody(status="done"), board=None)
    assert rejected.value.status_code in (400, 409)
    with pytest.raises(HTTPException):
        api.update_task(tid, api.UpdateTaskBody(title="bad", assignee="other"), board=None)
    assert kb.get_task(conn, tid).title == "draft"


def test_invalidated_submission_can_reopen_for_new_implementation(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    rp.patch_review_task(conn, tid, fields={"body": "new requirement"})
    assert rp.get_review_state(conn, tid)["submission"] is None
    assert kb.reopen_review_task(conn, tid)
    assert kb.get_task(conn, tid).assignee == "writer"
    run = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="new result", expected_run_id=run.current_run_id)
    assert rp.get_review_state(conn, tid)["submission"] is not None


def test_archiving_approved_task_does_not_unlock_result_edits(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    sub = rp.get_review_state(conn, tid)["submission"]["id"]
    rp.approve_task(conn, tid, actor_id="user", submission_id=sub, request_id="approve")
    assert kb.archive_task(conn, tid)
    with pytest.raises(ValueError, match="reopen"):
        kb.edit_task(conn, tid, body="archived edit")
    with pytest.raises(ValueError, match="reopen"):
        kb.store_attachment_bytes(conn, tid, "x.txt", b"archived edit")
    assert rp.get_review_state(conn, tid)["approval"] is not None


def test_agent_approval_retry_is_idempotent(conn, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    run = kb.claim_review_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="pass", expected_run_id=run.current_run_id)
    count = len(kb.list_events(conn, tid))
    assert kb.complete_task(conn, tid, summary="pass", expected_run_id=run.current_run_id)
    assert len(kb.list_events(conn, tid)) == count


def test_cross_board_worker_children_inherit_source_policy(conn, tmp_path, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    parent = kb.create_task(conn, title="parent", review_policy=HUMAN)
    with kbc.connect(tmp_path / "other.db") as other:
        imported = kb.create_task(other, title="imported legacy task")
        other.execute("UPDATE tasks SET id = ? WHERE id = ?", (parent, imported))
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_KANBAN_DB", kbc._main_db_file(conn))
        child = kb.create_task(other, title="child", creator_task_id=parent)
        assert rp.get_review_state(other, child)["policy"] == HUMAN
        with pytest.raises(ValueError):
            kb.complete_task(other, child, result="unapproved")


def test_read_state_reports_paused_or_unavailable_agent_reviewer(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp, profiles, kanban_db_dispatch as dispatch
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = submit(conn, AGENT)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: False)
    assert rp.get_review_state(conn, tid)["reason"] == "review_dispatch_disabled"
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    assert rp.get_review_state(conn, tid)["reason"] == "reviewer_unavailable"


def test_rejected_reassignment_does_not_reclaim_protected_worker(conn):
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN)
    worker = kb.claim_task(conn, tid)
    with pytest.raises(ValueError):
        kb.reassign_task(conn, tid, "other", reclaim_first=True)
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.current_run_id == worker.current_run_id
    assert task.claim_lock == worker.claim_lock


def test_cli_show_exposes_current_submission_for_operator_approval(conn, capsys):
    import json
    from argparse import Namespace
    from hermes_cli import kanban as cli, kanban_review_policy as rp
    tid = submit(conn)
    assert cli._cmd_show(Namespace(task_id=tid, json=True, recent=None, steps_only=False, attempts_only=False)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["review"]["submission"]["id"] == rp.get_review_state(conn, tid)["submission"]["id"]


def test_refused_result_edit_does_not_invalidate_submission(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    before = rp.get_review_state(conn, tid)
    assert kb.edit_task(conn, tid, result="not a permitted edit") is False
    assert rp.get_review_state(conn, tid) == before


def test_assignee_cannot_change_after_unclaimed_submission(conn):
    tid = kb.create_task(conn, title="draft", assignee="writer", review_policy=HUMAN)
    assert kb.request_review(conn, tid, summary="manual submission")
    with pytest.raises(ValueError, match="before execution"):
        kb.assign_task(conn, tid, "other")


def test_unresolvable_worker_origin_never_creates_legacy_child(conn, monkeypatch):
    from hermes_cli import kanban_review_policy as rp
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing_origin")
    tid = kb.create_task(conn, title="conservatively protected child")
    assert rp.get_review_state(conn, tid)["policy"] == HUMAN


def test_deleting_task_removes_current_review_state(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = kb.create_task(conn, title="temporary", review_policy=HUMAN)
    assert kb.delete_task(conn, tid)
    assert rp.get_review_state(conn, tid) is None


def test_human_receipt_preserves_display_name_without_changing_idempotence(conn):
    from hermes_cli import kanban_review_policy as rp
    tid = submit(conn)
    sub = rp.get_review_state(conn, tid)["submission"]["id"]
    args = dict(actor_id="user-uuid", submission_id=sub, request_id="click")
    approved = rp.approve_task(conn, tid, actor_name="Dante", **args)
    assert approved["approval"]["actor_name"] == "Dante"
    assert rp.approve_task(conn, tid, actor_name="Updated name", **args) == approved


def test_text_deliverable_handoff_and_review_context(conn, monkeypatch):
    from hermes_cli import profiles
    from tools.kanban_tools_schemas import KANBAN_REQUEST_REVIEW_SCHEMA
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(conn, title="Meeting notice", body="Write two Korean sentences.",
                         assignee="writer", review_policy=AGENT)
    context = kb.build_worker_context(conn, tid)
    assert "full text of the deliverable" in context
    assert "durable artifacts" in context
    description = KANBAN_REQUEST_REVIEW_SCHEMA["parameters"]["properties"]["summary"]["description"]
    assert "full text" in description
    assert "whole diff" in description
    run = kb.claim_task(conn, tid)
    deliverable = "회의 안건과 주요 쟁점을 정리해 주세요. 관련 참고 자료와 데이터를 준비해 주세요."
    assert kb.request_review(conn, tid, summary=deliverable, expected_run_id=run.current_run_id)
    assert kb.claim_review_task(conn, tid) is not None
    context = kb.build_worker_context(conn, tid)
    assert deliverable in context
    assert "completion claim is not evidence" in context
    assert "request changes" in context
    assert kb.get_task(conn, tid).result == deliverable
