"""Record which of our WhatsApp numbers a conversation is on.

A deployment can answer on more than one number, and they all arrive at the
same webhook. A reply has to go out from the number the customer wrote to -
anything else lands in a different thread on their phone - and a follow-up
sent days later has no webhook to read, only this row. So the number is stored
on the conversation, and it joins the open-conversation key: two of our numbers
are two separate threads, each with its own 24h service window.

Backfill: every existing conversation was necessarily on the single number the
deployment had at the time, which is the first entry of
`WHATSAPP_PHONE_NUMBER_IDS`. That must be set before this migration runs.

Revision ID: 7c1e4a9b2f30
Revises: 0371510ee02e
Create Date: 2026-09-02 10:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings

revision: str = "7c1e4a9b2f30"
down_revision: str | None = "0371510ee02e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_conversations_open_per_customer"
_WHERE = sa.text("status = 'open'")


def upgrade() -> None:
    existing_number = get_settings().default_phone_number_id
    if not existing_number:
        raise RuntimeError(
            "WHATSAPP_PHONE_NUMBER_IDS must be set before this migration runs: "
            "existing conversations are backfilled with the first entry."
        )

    # Added nullable so the backfill has somewhere to write, then tightened.
    op.add_column(
        "conversations", sa.Column("phone_number_id", sa.String(length=32), nullable=True)
    )
    op.execute(
        sa.text("UPDATE conversations SET phone_number_id = :number").bindparams(
            number=existing_number
        )
    )

    # The old index goes before the column is tightened, not after. SQLite has
    # no ALTER COLUMN, so `batch_alter_table` copies the table to change it -
    # and a partial index (`WHERE status = 'open'`) does not reliably survive
    # that copy. Dropping it first means there is nothing to survive, and the
    # new index is created against the finished column below.
    op.drop_index(_INDEX, table_name="conversations", postgresql_where=_WHERE, sqlite_where=_WHERE)

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "phone_number_id", existing_type=sa.String(length=32), nullable=False
        )

    op.create_index(
        _INDEX,
        "conversations",
        ["customer_id", "channel", "phone_number_id"],
        unique=True,
        postgresql_where=_WHERE,
        sqlite_where=_WHERE,
    )


def downgrade() -> None:
    # Note: this fails if any customer has an open conversation on more than
    # one number, and that is the correct outcome - two live threads cannot be
    # collapsed into one without silently losing a conversation. Close one
    # first if you genuinely need to go back.
    op.drop_index(_INDEX, table_name="conversations", postgresql_where=_WHERE, sqlite_where=_WHERE)
    op.create_index(
        _INDEX,
        "conversations",
        ["customer_id", "channel"],
        unique=True,
        postgresql_where=_WHERE,
        sqlite_where=_WHERE,
    )
    op.drop_column("conversations", "phone_number_id")
