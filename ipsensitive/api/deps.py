"""FastAPI dependencies: database session, prompt, model client and settings."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..moderation.model_client import ModelClient
from ..moderation.prompt import PromptSpec


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_prompt_dep(request: Request) -> PromptSpec:
    return request.app.state.prompt


def get_model_client_dep(request: Request) -> ModelClient:
    return request.app.state.model_client


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


__all__ = ["get_model_client_dep", "get_prompt_dep", "get_session", "get_settings_dep"]
