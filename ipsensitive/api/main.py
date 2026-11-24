"""Application factory.

``uvicorn --factory ipsensitive.api.main:create_app`` serves the service; tests
build an app with an explicit ``Settings`` and an injected model client, so no
test ever reaches a real model gateway.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from .. import __version__
from ..config import Settings, load_settings
from ..db import make_engine, make_session_factory
from ..moderation.model_client import ModelClient, build_model_client
from ..moderation.prompt import PromptSpec, load_prompt
from .routes import router
from .schemas import HealthOut


def create_app(
    *,
    settings: Settings | None = None,
    model_client: ModelClient | None = None,
    prompt: PromptSpec | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_prompt = prompt or load_prompt(resolved_settings.prompt_path)
    engine = make_engine(resolved_settings.database_url)
    session_factory = make_session_factory(engine)
    resolved_client = model_client or build_model_client(resolved_settings, resolved_prompt)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="IP sensitive re-review",
        description=(
            "Model re-review of sensitive content after the rule stage: masking gate, "
            "ip.sensitive.v1 prompt, merged rule+model flags in the staff review queue."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.prompt = resolved_prompt
    app.state.model_client = resolved_client
    app.state.session_factory = session_factory
    app.state.engine = engine

    app.include_router(router)

    @app.get("/healthz", response_model=HealthOut, tags=["ops"])
    def healthz() -> HealthOut:
        return HealthOut(
            status="ok",
            prompt_version=resolved_prompt.version,
            prompt_batch_size=resolved_prompt.batch_size,
            model_mode=resolved_settings.model_mode,
            min_model_confidence=resolved_settings.min_model_confidence,
        )

    return app


__all__ = ["create_app"]
