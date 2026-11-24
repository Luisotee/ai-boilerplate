"""Group tasks scheduled from a private chat, against a real database (SQLite in memory).

What mocks can't show: a disabling creator verdict ends the task while a failed
lookup only skips the run (and re-arms a one-shot), ``/clean all`` takes back
the requester's group tasks, and a /link merge re-points them instead of letting
the FK's SET NULL turn them into ordinary group tasks.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import create_engine  # NOT sqlalchemy.create_engine (conftest patch)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_api.database import Base, ScheduledTask, TaskRun, User
from ai_api.services import scheduled_tasks as svc
from ai_api.services import task_creator
from ai_api.streams import scheduled_runner as runner

PRIVATE = "5511999999999@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(
        engine, tables=[User.__table__, ScheduledTask.__table__, TaskRun.__table__]
    )
    make = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runner, "SessionLocal", make)
    monkeypatch.setattr(task_creator, "SessionLocal", make)
    yield make
    engine.dispose()


def _setup(db, *, one_shot=False, created_from=True):
    requester = User(whatsapp_jid=PRIVATE, conversation_type="private")
    group = User(whatsapp_jid=GROUP, conversation_type="group", name="Book Club")
    db.add_all([requester, group])
    db.flush()
    task = ScheduledTask(
        user_id=group.id,
        address=GROUP,
        kind="reminder",
        text="Meeting at 5",
        schedule="every day at 17:00",
        cron_expr=None if one_shot else "0 17 * * *",
        run_at=datetime(2026, 11, 25, 20, 0) if one_shot else None,
        timezone="America/Sao_Paulo",
        status="active",
        next_run_at=datetime(2026, 11, 25, 20, 0),
        consecutive_failures=0,
        run_count=0,
        created_from_user_id=requester.id if created_from else None,
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        user_id=group.id,
        scheduled_for=task.next_run_at,
        status=runner.RUNNING,
        late_seconds=0,
    )
    db.add(run)
    db.commit()
    return requester, group, task, run


class TestRecordCreatorVerdict:
    @pytest.mark.parametrize("verdict", sorted(task_creator.DISABLING))
    def test_a_disabling_verdict_ends_the_task(self, factory, verdict):
        db = factory()
        _r, _g, task, run = _setup(db)
        assert runner.record_creator_verdict(run.id, runner.RUNNING, verdict)
        db.expire_all()
        task = db.get(ScheduledTask, task.id)
        assert (task.status, task.disabled_reason, task.next_run_at) == ("disabled", verdict, None)
        run = db.get(TaskRun, run.id)
        assert (run.status, run.error_code) == ("skipped", verdict)

    def test_a_failed_lookup_only_skips_a_recurring_run(self, factory):
        db = factory()
        _r, _g, task, run = _setup(db)
        runner.record_creator_verdict(run.id, runner.RUNNING, task_creator.CREATOR_CHECK_FAILED)
        db.expire_all()
        task = db.get(ScheduledTask, task.id)
        assert task.status == "active" and task.consecutive_failures == 0
        assert db.get(TaskRun, run.id).error_code == "creator_check_failed"

    def test_a_failed_lookup_re_arms_a_one_shot(self, factory):
        """A transient failure must not end a one-shot: it is retried, bounded."""
        db = factory()
        _r, _g, task, run = _setup(db, one_shot=True)
        before = datetime.utcnow()
        runner.record_creator_verdict(run.id, runner.RUNNING, task_creator.CREATOR_CHECK_FAILED)
        db.expire_all()
        task = db.get(ScheduledTask, task.id)
        assert task.status == "active"
        assert task.next_run_at >= before + runner.ONE_SHOT_RETRY_DELAY - timedelta(seconds=5)

    def test_a_one_shot_stops_retrying_after_its_attempts(self, factory):
        db = factory()
        _r, group, task, run = _setup(db, one_shot=True)
        for i in range(runner.ONE_SHOT_ATTEMPTS - 1):
            db.add(
                TaskRun(
                    task_id=task.id,
                    user_id=group.id,
                    scheduled_for=task.next_run_at + timedelta(minutes=i + 1),
                    status="skipped",
                    late_seconds=0,
                )
            )
        db.commit()
        runner.record_creator_verdict(run.id, runner.RUNNING, task_creator.CREATOR_CHECK_FAILED)
        db.expire_all()
        assert db.get(ScheduledTask, task.id).status == "disabled"

    def test_a_settled_run_is_not_rewritten(self, factory):
        db = factory()
        _r, _g, _task, run = _setup(db)
        assert not runner.record_creator_verdict(run.id, runner.SENDING, "creator_not_admin")

    async def test_the_verdict_reads_the_task(self, factory, monkeypatch):
        db = factory()
        requester, _g, task, _run = _setup(db)
        seen = {}

        async def status(_client, platform, user, group_jid):
            seen.update(platform=platform, user=user.id, group=group_jid)
            return True, False

        monkeypatch.setattr(task_creator, "requester_group_status", status)
        verdict = await task_creator.creator_verdict(task.id)
        assert verdict == task_creator.CREATOR_NOT_ADMIN
        assert seen == {"platform": "baileys", "user": requester.id, "group": GROUP}

    async def test_an_ordinary_group_task_is_never_checked(self, factory):
        db = factory()
        _r, _g, task, _run = _setup(db, created_from=False)
        assert await task_creator.creator_verdict(task.id) is None


def _next_run(db, task_id):
    """A new claimed run of the task (the scheduler's next fire)."""
    task = db.get(ScheduledTask, task_id)
    last = max(r.scheduled_for for r in db.query(TaskRun).filter(TaskRun.task_id == task_id))
    run = TaskRun(
        task_id=task.id,
        user_id=task.user_id,
        scheduled_for=last + timedelta(minutes=1),
        status=runner.RUNNING,
        late_seconds=0,
    )
    db.add(run)
    db.commit()
    return run


class TestGroupMissingFromSharedGroups:
    """Baileys leaves a group out when it can't resolve a LID: disable on the 2nd miss only."""

    @pytest.fixture
    def missing(self, monkeypatch):
        async def status(_client, _platform, _user, _group_jid):
            return False, False

        monkeypatch.setattr(task_creator, "requester_group_status", status)

    async def _check(self, factory, task_id, run_id):
        verdict = await task_creator.creator_verdict(task_id)
        assert verdict is not None
        assert runner.record_creator_verdict(run_id, runner.RUNNING, verdict)
        return verdict

    async def test_recurring_first_miss_skips_second_disables(self, factory, missing):
        db = factory()
        _r, _g, task, run = _setup(db)
        assert await self._check(factory, task.id, run.id) == task_creator.CREATOR_NOT_SEEN
        db.expire_all()
        row = db.get(ScheduledTask, task.id)
        assert (row.status, row.last_error, row.consecutive_failures) == (
            "active",
            "creator_not_seen",
            0,
        )
        second = _next_run(db, task.id)
        assert await self._check(factory, task.id, second.id) == task_creator.CREATOR_NOT_IN_GROUP
        db.expire_all()
        row = db.get(ScheduledTask, task.id)
        assert (row.status, row.disabled_reason) == ("disabled", "creator_not_in_group")

    async def test_a_run_in_between_resets_the_miss(self, factory, missing, monkeypatch):
        db = factory()
        _r, _g, task, run = _setup(db)
        await self._check(factory, task.id, run.id)
        db.expire_all()
        ok = _next_run(db, task.id)
        assert runner.record(ok.id, runner.RUNNING, runner.SENT)
        third = _next_run(db, task.id)
        assert await self._check(factory, task.id, third.id) == task_creator.CREATOR_NOT_SEEN
        db.expire_all()
        assert db.get(ScheduledTask, task.id).status == "active"

    async def test_a_pass_clears_the_miss_so_a_later_miss_only_skips(self, factory, monkeypatch):
        """Miss (run N), pass in the runner (run N+1), miss in the sender (run N+1): skip."""
        db = factory()
        _r, _g, task, run = _setup(db)
        answers = iter([(False, False), (True, True), (False, False)])

        async def status(_client, _platform, _user, _group_jid):
            return next(answers)

        monkeypatch.setattr(task_creator, "requester_group_status", status)
        assert await self._check(factory, task.id, run.id) == task_creator.CREATOR_NOT_SEEN
        second = _next_run(db, task.id)
        assert await task_creator.creator_verdict(task.id) is None  # the runner's check
        db.expire_all()
        assert db.get(ScheduledTask, task.id).last_error is None
        db.query(TaskRun).filter(TaskRun.id == second.id).update({"status": runner.SENDING})
        db.commit()
        verdict = await task_creator.creator_verdict(task.id)  # the sender's check
        assert verdict == task_creator.CREATOR_NOT_SEEN
        assert runner.record_creator_verdict(second.id, runner.SENDING, verdict)
        db.expire_all()
        assert db.get(ScheduledTask, task.id).status == "active"

    async def test_a_pass_leaves_another_last_error_alone(self, factory, monkeypatch):
        db = factory()
        _r, _g, task, _run = _setup(db)
        db.get(ScheduledTask, task.id).last_error = "blocked"
        db.commit()

        async def status(_client, _platform, _user, _group_jid):
            return True, True

        monkeypatch.setattr(task_creator, "requester_group_status", status)
        assert await task_creator.creator_verdict(task.id) is None
        db.expire_all()
        assert db.get(ScheduledTask, task.id).last_error == "blocked"

    async def test_one_shot_never_escalates_and_ends_after_its_retries(self, factory, missing):
        """Each miss of a one-shot is creator_not_seen; its bounded retries then end it."""
        db = factory()
        _r, _g, task, run = _setup(db, one_shot=True)
        before = datetime.utcnow()
        assert await self._check(factory, task.id, run.id) == task_creator.CREATOR_NOT_SEEN
        db.expire_all()
        row = db.get(ScheduledTask, task.id)
        assert row.status == "active"
        assert row.next_run_at >= before + runner.ONE_SHOT_RETRY_DELAY - timedelta(seconds=5)
        for _ in range(runner.ONE_SHOT_ATTEMPTS - 1):
            nxt = _next_run(db, task.id)
            assert await self._check(factory, task.id, nxt.id) == task_creator.CREATOR_NOT_SEEN
        db.expire_all()
        row = db.get(ScheduledTask, task.id)
        assert (row.status, row.disabled_reason) == ("disabled", "creator_not_seen")


class TestRowLifecycle:
    def test_clean_all_cancels_the_requesters_group_tasks(self, factory):
        db = factory()
        requester, _g, task, _run = _setup(db)
        assert svc.cancel_chat_tasks(db, requester.id) == 1
        db.commit()
        assert db.get(ScheduledTask, task.id).status == "cancelled"

    def test_a_link_merge_re_points_the_creator(self, factory):
        db = factory()
        requester, _g, task, _run = _setup(db)
        kept = User(whatsapp_jid="tg:77", conversation_type="private")
        db.add(kept)
        db.commit()
        assert svc.repoint_created_from(db, requester.id, kept.id) == 1
        db.commit()
        assert db.get(ScheduledTask, task.id).created_from_user_id == kept.id

    def test_a_pure_delete_disables_them(self, factory):
        db = factory()
        requester, _g, task, _run = _setup(db)
        assert svc.disable_created_from(db, requester.id) == 1
        db.commit()
        task = db.get(ScheduledTask, task.id)
        assert (task.status, task.disabled_reason) == ("disabled", "creator_unknown")

    def test_private_list_and_cancel_reach_the_group_task(self, factory):
        db = factory()
        requester, _g, task, _run = _setup(db)
        assert [t.id for t in svc.list_tasks_created_from(db, requester.id)] == [task.id]
        ref = str(task.id).replace("-", "")[:8]
        assert svc.find_task(db, None, ref, created_from=requester.id).id == task.id
        with pytest.raises(svc.TaskNotFound):
            svc.find_task(db, requester.id, ref)  # not this chat's own task

    def test_an_ambiguous_id_is_not_a_miss(self, factory):
        """Two of the chat's own tasks share the prefix: TaskError, never TaskNotFound."""
        db = factory()
        requester, _g, task, _run = _setup(db)
        ids = ["ab12cd34-0000-0000-0000-000000000001", "ab12cd34-0000-0000-0000-000000000002"]
        for raw in ids:
            db.add(
                ScheduledTask(
                    id=uuid.UUID(raw),
                    user_id=requester.id,
                    address=PRIVATE,
                    kind="reminder",
                    text="x",
                    schedule="every day at 9:00",
                    cron_expr="0 9 * * *",
                    timezone="America/Sao_Paulo",
                    status="active",
                    next_run_at=datetime(2026, 11, 25, 12, 0),
                    consecutive_failures=0,
                    run_count=0,
                )
            )
        db.commit()
        with pytest.raises(svc.TaskError) as raised:
            svc.cancel_task(db, requester.id, "ab12")
        assert not isinstance(raised.value, svc.TaskNotFound)
