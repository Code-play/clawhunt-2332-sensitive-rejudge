"""Shared fixtures.

The database-driven tests run against a real PostgreSQL 16 instance
(``TEST_DATABASE_URL``, default the ``docker compose`` database).  The schema is
created by running the Alembic migration, so each run also proves the migration
works.  Pure unit tests (prompt, masking, batching, parsing, merge) never touch
the database.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ipsensitive.api.main import create_app
from ipsensitive.config import Settings
from ipsensitive.db import make_engine, make_session_factory
from ipsensitive.moderation.model_client import RecordedModelClient
from ipsensitive.moderation.prompt import PromptSpec, load_prompt

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = (
    os.getenv("TEST_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or "postgresql+psycopg://ipsensitive:ipsensitive_local_dev@127.0.0.1:55432/ipsensitive"
)
# migrations/env.py prefers DATABASE_URL; align it with the test database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


@pytest.fixture(scope="session")
def alembic_cfg() -> Config:
    return alembic_config()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = make_engine(TEST_DATABASE_URL)
    with eng.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(), "head")
    yield eng
    eng.dispose()


@pytest.fixture()
def clean_database(engine: Engine) -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE ip_sensitive_flags CASCADE"))
    yield


@pytest.fixture()
def session_factory(clean_database: None, engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def tenant() -> dict[str, uuid.UUID]:
    return {"org_id": uuid.uuid4(), "site_id": uuid.uuid4()}


@pytest.fixture(scope="session")
def prompt() -> PromptSpec:
    return load_prompt(ROOT / "prompts" / "ip.sensitive.v1.md")


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        prompt_path=ROOT / "prompts" / "ip.sensitive.v1.md",
        model_mode="recorded",
        model_replay_path=ROOT / "evalset" / "model_output_v1.json",
        min_model_confidence=0.5,
    )


@pytest.fixture()
def model_output_path() -> Path:
    return ROOT / "evalset" / "model_output_v1.json"


@pytest.fixture()
def recorded_client(model_output_path: Path) -> RecordedModelClient:
    return RecordedModelClient.from_file(model_output_path)


@pytest.fixture()
def make_client() -> Callable[..., RecordedModelClient]:
    """Build a replay client; a mapping value is serialised to JSON text."""

    def _make(responses: dict[str, object], model_id: str = "test-model-v1") -> RecordedModelClient:
        serialised = {
            key: (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            for key, value in responses.items()
        }
        return RecordedModelClient(serialised, model_id=model_id)

    return _make


@pytest.fixture()
def api_client_factory(
    settings: Settings, prompt: PromptSpec, session_factory: sessionmaker[Session]
) -> Iterator[Callable[[RecordedModelClient], TestClient]]:
    apps = []

    def _make(model_client: RecordedModelClient) -> TestClient:
        app = create_app(settings=settings, model_client=model_client, prompt=prompt)
        apps.append(app)
        return TestClient(app)

    yield _make
    for app in apps:
        app.state.engine.dispose()
