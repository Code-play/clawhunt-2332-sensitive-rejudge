"""ip_sensitive_flags - review queue of the Personal IP sensitive re-review

Revision ID: 0001_ip_sensitive_flags
Revises:
Create Date: 2026-11-24

The table follows the platform convention (id UUID, org_id, site_id,
created_at, created_by, updated_at, deleted_at) and enforces the ticket's merge
contract in the database: one live row per fragment (unique expression index),
allowed detector/flag_type/disposition values, confidence in 0..1, reasons of at
most 40 characters, and a review state that cannot be half-set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_ip_sensitive_flags"
down_revision = None
branch_labels = None
depends_on = None

FLAG_TYPES_SQL = "('third_party_personal', 'unpublished_business', 'unlicensed_brand', 'other')"
DETECTORS_SQL = "('rule', 'model', 'rule+model')"
DISPOSITIONS_SQL = "('pending', 'accepted', 'dismissed')"


def upgrade() -> None:
    op.create_table(
        "ip_sensitive_flags",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("site_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("content_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("segment_seq", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("flag_type", sa.String(length=32), nullable=False),
        sa.Column("detector", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("rule_code", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("disposition", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("reviewed_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("segment_seq >= 0", name="ck_ip_sensitive_flags_seq"),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start AND quote_end - quote_start <= 200",
            name="ck_ip_sensitive_flags_span",
        ),
        sa.CheckConstraint(f"flag_type IN {FLAG_TYPES_SQL}", name="ck_ip_sensitive_flags_flag_type"),
        sa.CheckConstraint(f"detector IN {DETECTORS_SQL}", name="ck_ip_sensitive_flags_detector"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ip_sensitive_flags_confidence"),
        sa.CheckConstraint("char_length(reason) BETWEEN 1 AND 40", name="ck_ip_sensitive_flags_reason_len"),
        sa.CheckConstraint(f"disposition IN {DISPOSITIONS_SQL}", name="ck_ip_sensitive_flags_disposition"),
        sa.CheckConstraint(
            "(disposition = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL)"
            " OR (disposition <> 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_ip_sensitive_flags_review_state",
        ),
        sa.CheckConstraint("char_length(quote) BETWEEN 1 AND 200", name="ck_ip_sensitive_flags_quote_len"),
    )
    op.create_index("ix_ip_sensitive_flags_run", "ip_sensitive_flags", ["run_id"])
    op.create_index(
        "ix_ip_sensitive_flags_queue",
        "ip_sensitive_flags",
        ["org_id", "site_id", "content_id", "disposition"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_ip_sensitive_flags_fragment",
        "ip_sensitive_flags",
        ["content_id", "segment_seq", sa.text("md5(quote)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_ip_sensitive_flags_fragment", table_name="ip_sensitive_flags")
    op.drop_index("ix_ip_sensitive_flags_queue", table_name="ip_sensitive_flags")
    op.drop_index("ix_ip_sensitive_flags_run", table_name="ip_sensitive_flags")
    op.drop_table("ip_sensitive_flags")
