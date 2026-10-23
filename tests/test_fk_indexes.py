"""Every foreign key column is covered by an index that leads with it (PostgreSQL does not create
these itself, so an unindexed one turns each parent-row delete into a full scan of the child table)."""
from __future__ import annotations

from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint


def test_every_foreign_key_has_a_leading_index():
    from app import models  # noqa: F401
    from app.database import Base

    missing = []
    for table in Base.metadata.tables.values():
        leading = {next(iter(ix.columns)).name for ix in table.indexes if len(ix.columns)}
        leading |= {next(iter(c.columns)).name for c in table.constraints
                    if isinstance(c, PrimaryKeyConstraint | UniqueConstraint) and len(c.columns)}
        missing += [f"{table.name}.{next(iter(fk.columns)).name}" for fk in table.foreign_key_constraints
                    if next(iter(fk.columns)).name not in leading]
    assert not missing, f"foreign keys without an index: {missing}"


def test_the_frequently_deleted_parents_are_covered():
    from app import models  # noqa: F401
    from app.database import Base

    names = {ix.name for t in Base.metadata.tables.values() for ix in t.indexes}
    assert {"ix_comments_author_user_id", "ix_notifications_bug_id", "ix_attachments_uploader_user_id"} <= names
