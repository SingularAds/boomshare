"""What made a reply take the better part of a minute, and what stops it.

Four separate defects added up to the production symptom, and each one has a
test here that fails if it comes back:

  * a database connection was held for the whole model call, so a handful of
    simultaneous customers could occupy the entire pool
  * the worker ran one job at a time, so every customer queued behind whichever
    model call happened to be in flight
  * a customer's second message was parked until the next sweep instead of
    waiting a moment for the first to finish
  * the sweeper's batch filled up with events it could never process, which
    starved the recovery path it exists to provide

They are grouped by symptom rather than by module, because that is how they will
be recognised if they return.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import event as sa_event, select

from app.core import redis as redis_helper
from app.core.clock import utcnow
from app.core.errors import PermanentError
from app.domain import (
    MessageDirection,
    MessageStatus,
    WebhookEventType,
    WebhookStatus,
)
from app.models import Conversation, Message, WebhookEvent
from app.services import conversations as conversation_service
from app.worker import queue
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post(client, **kwargs) -> None:
    """Deliver one correctly signed inbound message."""
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestTheModelCallHoldsNoDatabaseConnection:
    """The pool is small and a model call is seconds long.

    Holding a connection across it is what turned a handful of simultaneous
    customers into a pool exhaustion, and a pool exhaustion into a webhook
    endpoint that blocked for the full checkout timeout before it could even
    store the message.
    """

    async def test_nothing_is_checked_out_while_the_model_thinks(
        self, client, engine, ai, meta
    ):
        checked_out = 0
        observed: list[int] = []

        @sa_event.listens_for(engine.sync_engine, "checkout")
        def _out(*_args: object) -> None:
            nonlocal checked_out
            checked_out += 1

        @sa_event.listens_for(engine.sync_engine, "checkin")
        def _in(*_args: object) -> None:
            nonlocal checked_out
            checked_out -= 1

        async def _look() -> None:
            observed.append(checked_out)

        ai.on_decide = _look

        await post(client, text="hello")
        await drain_queue()

        assert observed, "the model was never called - the test proved nothing"
        assert observed == [0] * len(observed), (
            "a database connection was held across the model call "
            f"(checked out: {observed}). The reply path must read its inputs, "
            "commit, and only then call the model."
        )
        assert len(meta.texts) == 1, "the reply still has to go out"


class TestUnrelatedConversationsAreAnsweredTogether:
    """Two customers who message at the same moment are two independent turns.

    Nothing about answering one requires waiting for the other's model call to
    come back.
    """

    async def test_two_customers_are_in_the_model_at_once(self, client, db, ai, meta):
        from app.worker.runner import consume

        both_in_flight = asyncio.Event()
        in_flight = 0

        async def _wait_for_the_other() -> None:
            nonlocal in_flight
            in_flight += 1
            if in_flight >= 2:
                both_in_flight.set()
            # A serial consumer never reaches this a second time, so it times
            # out here and the test fails with the reason rather than by being
            # slow.
            await asyncio.wait_for(both_in_flight.wait(), timeout=5)

        ai.on_decide = _wait_for_the_other

        await post(client, text="hello", wa_id="919111000001", profile_name="Asha")
        await post(client, text="hello", wa_id="919111000002", profile_name="Ben")

        stop = asyncio.Event()
        task = asyncio.create_task(consume(stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if both_in_flight.is_set() and await queue.depth() == 0:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=10)

        assert both_in_flight.is_set(), (
            "the second customer's turn never started while the first was still "
            "in the model - the consumer is running jobs one at a time"
        )
        assert len(meta.texts) == 2, "both customers must be answered"
        assert {m.to for m in meta.texts} == {"919111000001", "919111000002"}


class TestASecondMessageDoesNotWaitForASweep:
    """A customer who sends two messages in a row is the ordinary case.

    The second one has to queue behind the first - two overlapping replies to
    one conversation is the thing the lock exists to prevent - but queueing is
    a wait, not a failure. Failing parks the job, burns one of
    `worker_max_attempts`, and leaves the answer until the next sweep.
    """

    async def test_a_held_lock_is_waited_out_rather_than_abandoned(
        self, client, db, ai, meta, redis
    ):
        await post(client, text="hi", wa_id="919111000111")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        lock_key = f"lock:conversation:{conversation.id}"

        # Another worker is mid-reply on this conversation.
        await redis.set(lock_key, "another-worker", ex=30)

        async def _finish_and_release() -> None:
            await asyncio.sleep(0.3)
            await redis.delete(lock_key)

        await post(client, text="and one more thing", wa_id="919111000111")
        releaser = asyncio.create_task(_finish_and_release())
        await drain_queue()
        await releaser

        assert len(meta.texts) == 2, "the second message must be answered too"

        events = (await db.execute(select(WebhookEvent))).scalars().all()
        assert [e.status for e in events] == [WebhookStatus.PROCESSED] * len(events), (
            "a message that merely waited its turn must not be parked as failed"
        )
        assert all(e.attempts == 1 for e in events), (
            "waiting for the lock must not burn a retry attempt"
        )

    async def test_without_a_wait_the_lock_still_refuses_immediately(self, redis):
        """The default is unchanged: callers that do not ask to wait, do not."""
        async with redis_helper.lock("shared") as first:
            assert first
            async with redis_helper.lock("shared") as second:
                assert not second

    async def test_a_waiting_caller_acquires_once_the_holder_releases(self, redis):
        await redis.set("lock:shared", "held", ex=30)

        async def _release() -> None:
            await asyncio.sleep(0.2)
            await redis.delete("lock:shared")

        releaser = asyncio.create_task(_release())
        async with redis_helper.lock("shared", wait_seconds=5, poll_seconds=0.05) as acquired:
            assert acquired
        await releaser

    async def test_a_waiting_caller_still_gives_up_when_the_budget_runs_out(self, redis):
        """The wait is bounded, so a dead holder cannot block a worker forever."""
        await redis.set("lock:shared", "held", ex=30)
        async with redis_helper.lock("shared", wait_seconds=0.3, poll_seconds=0.05) as acquired:
            assert not acquired


class TestDeliveryReceiptsAreOrderIndependent:
    """Meta sends `sent`, `delivered` and `read` as three separate webhooks.

    Nothing guarantees they finish processing in the order they were sent, and
    once the worker runs jobs concurrently, two of them are genuinely in flight
    at the same time.
    """

    async def _outbound_id(self, client, db) -> str:
        await post(client, text="hi")
        await drain_queue()
        message = (
            await db.execute(
                select(Message).where(Message.direction == MessageDirection.OUTBOUND)
            )
        ).scalars().first()
        return message.provider_message_id

    async def _apply(self, session_factory, provider_message_id: str, status: str) -> None:
        async with session_factory() as session:
            await conversation_service.apply_delivery_status(
                session, provider_message_id, status
            )
            await session.commit()

    async def test_a_late_sent_receipt_does_not_demote_a_read_message(
        self, client, db, ai, meta, session_factory
    ):
        provider_message_id = await self._outbound_id(client, db)

        # Arriving backwards, which is exactly what concurrency permits.
        await self._apply(session_factory, provider_message_id, "read")
        await self._apply(session_factory, provider_message_id, "delivered")
        await self._apply(session_factory, provider_message_id, "sent")

        message = (
            await db.execute(
                select(Message).where(Message.provider_message_id == provider_message_id)
            )
        ).scalar_one()
        assert message.status == MessageStatus.READ, (
            "a receipt may only move a message forward"
        )
        assert message.read_at is not None
        assert message.delivered_at is not None, (
            "a message that was read was necessarily delivered"
        )

    async def test_in_order_receipts_still_land_exactly_as_before(
        self, client, db, ai, meta, session_factory
    ):
        provider_message_id = await self._outbound_id(client, db)

        await self._apply(session_factory, provider_message_id, "sent")
        await self._apply(session_factory, provider_message_id, "delivered")
        await self._apply(session_factory, provider_message_id, "read")

        message = (
            await db.execute(
                select(Message).where(Message.provider_message_id == provider_message_id)
            )
        ).scalar_one()
        assert message.status == MessageStatus.READ
        assert message.delivered_at is not None
        assert message.read_at is not None

    async def test_a_failure_is_not_cleared_by_a_stray_receipt(
        self, client, db, ai, meta, session_factory
    ):
        """Meta sends `failed` *instead of* delivery. It is terminal."""
        provider_message_id = await self._outbound_id(client, db)

        await self._apply(session_factory, provider_message_id, "failed")
        await self._apply(session_factory, provider_message_id, "delivered")

        message = (
            await db.execute(
                select(Message).where(Message.provider_message_id == provider_message_id)
            )
        ).scalar_one()
        assert message.status == MessageStatus.FAILED


class TestTheSweeperOnlyLooksAtUnfinishedWork:
    """`mark_failed(retryable=False)` parks an event by stamping `processed_at`.

    `claim` has always refused those rows. The sweeper did not check, so it
    re-enqueued them on every pass - forever, because nothing could ever move
    them on.
    """

    async def test_a_permanently_failed_event_is_never_swept_up_again(
        self, client, db, ai, meta, monkeypatch, settings
    ):
        from app.worker.runner import sweep_stuck_webhooks

        async def _refuse(_event: object) -> None:
            raise PermanentError("this payload will never work")

        monkeypatch.setattr(
            "app.services.conversation_flow.handle_inbound_message", _refuse
        )

        await post(client, text="hello")
        await drain_queue()

        stored = (await db.execute(select(WebhookEvent))).scalar_one()
        assert stored.status == WebhookStatus.FAILED
        assert stored.processed_at is not None, "a permanent failure is parked"
        assert stored.attempts < settings.worker_max_attempts, (
            "and it is parked well before the attempt cap, which is what made "
            "the sweeper's attempts filter miss it"
        )

        async def _age(session):
            row = await session.get(WebhookEvent, stored.id)
            row.received_at = utcnow() - timedelta(hours=1)

        await db.write(_age)

        assert await sweep_stuck_webhooks() == 0
        assert await queue.depth() == 0, "nothing should have been re-enqueued"

    async def test_parked_events_cannot_crowd_out_a_genuinely_stuck_one(
        self, db, redis, settings
    ):
        """The batch is ordered oldest-first and capped at `worker_batch_size`.

        Enough parked events and the sweep never sees anything else - which
        disabled recovery entirely, silently.
        """
        from app.services import webhook_events

        old = utcnow() - timedelta(hours=2)

        async def _seed(session):
            for index in range(settings.worker_batch_size + 5):
                session.add(
                    WebhookEvent(
                        provider="meta",
                        event_key=f"parked-{index}",
                        event_type=WebhookEventType.WHATSAPP_MESSAGE,
                        payload={},
                        status=WebhookStatus.FAILED,
                        attempts=1,
                        # Older than the stuck one, so oldest-first ordering
                        # puts every one of them ahead of it.
                        received_at=old - timedelta(minutes=index + 1),
                        processed_at=old,
                    )
                )
            session.add(
                WebhookEvent(
                    provider="meta",
                    event_key="genuinely-stuck",
                    event_type=WebhookEventType.WHATSAPP_MESSAGE,
                    payload={},
                    status=WebhookStatus.PENDING,
                    attempts=0,
                    received_at=old,
                    processed_at=None,
                )
            )

        await db.write(_seed)

        stuck = await db.execute(
            select(WebhookEvent.id).where(WebhookEvent.event_key == "genuinely-stuck")
        )
        stuck_id = stuck.scalar_one()

        found = await db.write(
            lambda session: webhook_events.stale_event_ids(
                session, limit=settings.worker_batch_size
            )
        )

        assert found == [stuck_id], (
            "the sweep must return the one event that still has work left, not "
            "a batch of parked ones it can never process"
        )
