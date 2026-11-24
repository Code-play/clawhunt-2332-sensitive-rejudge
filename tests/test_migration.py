"""Schema guarantees: the migration is the schema, and it is reversible."""

from __future__ import annotations

from alembic import command
from sqlalchemy import Engine, text

HEAD = "0001_ip_sensitive_flags"

PLATFORM_COLUMNS = {
    "id",
    "org_id",
    "site_id",
    "created_at",
    "created_by",
    "updated_at",
    "deleted_at",
}

FLAG_COLUMNS = {
    "content_id",
    "run_id",
    "segment_seq",
    "quote",
    "quote_start",
    "quote_end",
    "flag_type",
    "detector",
    "confidence",
    "reason",
    "rule_code",
    "prompt_version",
    "disposition",
    "reviewed_by",
    "reviewed_at",
    "review_note",
}

CHECK_CONSTRAINTS = {
    "ck_ip_sensitive_flags_seq",
    "ck_ip_sensitive_flags_span",
    "ck_ip_sensitive_flags_flag_type",
    "ck_ip_sensitive_flags_detector",
    "ck_ip_sensitive_flags_confidence",
    "ck_ip_sensitive_flags_reason_len",
    "ck_ip_sensitive_flags_disposition",
    "ck_ip_sensitive_flags_review_state",
    "ck_ip_sensitive_flags_quote_len",
}


def test_database_is_at_the_migration_head(engine: Engine):
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_columns_follow_the_platform_convention(engine: Engine):
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ip_sensitive_flags'"
                )
            )
        }
    assert PLATFORM_COLUMNS <= columns
    assert FLAG_COLUMNS <= columns


def test_check_constraints_are_enforced_by_postgresql(engine: Engine):
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'ip_sensitive_flags'::regclass AND contype = 'c'"
                )
            )
        }
    assert CHECK_CONSTRAINTS <= names


def test_disposition_defaults_to_pending_at_the_database_level(engine: Engine):
    with engine.connect() as connection:
        default = connection.scalar(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'ip_sensitive_flags' AND column_name = 'disposition'"
            )
        )
    assert default is not None and "pending" in default


def test_indexes_cover_the_queue_and_the_fragment_uniqueness(engine: Engine):
    with engine.connect() as connection:
        definitions = {
            row[0]: row[1]
            for row in connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ip_sensitive_flags'")
            )
        }
    assert "ix_ip_sensitive_flags_queue" in definitions
    assert "ix_ip_sensitive_flags_run" in definitions
    unique = definitions["uq_ip_sensitive_flags_fragment"]
    assert "UNIQUE INDEX" in unique
    assert "md5(quote)" in unique
    assert "WHERE (deleted_at IS NULL)" in unique


def test_rows_inserted_without_a_disposition_are_pending(engine: Engine):
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE ip_sensitive_flags CASCADE"))
        connection.execute(
            text(
                "INSERT INTO ip_sensitive_flags "
                "(id, org_id, site_id, content_id, run_id, segment_seq, quote, quote_start, quote_end, "
                " flag_type, detector, confidence, reason) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
                "        gen_random_uuid(), 0, '片段', 0, 2, 'other', 'model', 0.5, '默认值检查')"
            )
        )
        assert connection.scalar(text("SELECT disposition FROM ip_sensitive_flags")) == "pending"


def test_migration_roundtrip_recreates_the_schema(engine: Engine, alembic_cfg):
    command.downgrade(alembic_cfg, "base")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('ip_sensitive_flags')")) is None
    command.upgrade(alembic_cfg, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('ip_sensitive_flags')")) == "ip_sensitive_flags"
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_no_model_migration_drift(alembic_cfg):
    command.check(alembic_cfg)
