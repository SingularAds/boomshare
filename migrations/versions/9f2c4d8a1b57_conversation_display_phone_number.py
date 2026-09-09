"""Store the WhatsApp number as a person would dial it.

`phone_number_id` identifies which of our numbers a thread is on, but it is an
opaque Meta id: it tells an operator reading the dashboard nothing. Meta sends
the readable number beside that id in the metadata of every webhook, and we
were discarding it - so the dashboard had to fall back on hand-maintained
configuration, which goes stale the moment a number is added.

Nullable, and deliberately not backfilled: a thread opened before this existed
never saw that webhook, and inventing a number for it would be worse than an
honest gap. Existing threads fill themselves in on the customer's next message.

Revision ID: 9f2c4d8a1b57
Revises: 7c1e4a9b2f30
Create Date: 2026-09-09 15:50:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f2c4d8a1b57"
down_revision: str | None = "7c1e4a9b2f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("display_phone_number", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "display_phone_number")
