from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .hubspot import HubSpotClient, HubSpotError
from .lead_agent import LeadQualificationAgent
from .lead_config import Settings
from .lead_logic import ExtractionError, OpenAICompanyFactExtractor
from .lead_models import LeadRequest, LeadResponse
from .lead_storage import LeadStorage
from .website import WebsiteFetcher, WebsiteFetchError

logger = logging.getLogger("lead_agent.http")
bearer_scheme = HTTPBearer(auto_error=False)


def build_agent(settings: Settings) -> LeadQualificationAgent:
    storage = LeadStorage(settings.database_url, initialize_schema=not settings.database_url.startswith("postgresql"))
    return LeadQualificationAgent(
        fetcher=WebsiteFetcher(timeout=settings.website_timeout_seconds, max_bytes=settings.max_website_bytes),
        extractor=OpenAICompanyFactExtractor(settings.openai_api_key.get_secret_value(), settings.openai_model),
        hubspot=HubSpotClient(settings.hubspot_access_token.get_secret_value(), settings.hubspot_api_version),
        storage=storage,
        policy=settings.load_policy(),
        daily_model_call_limit=settings.daily_model_call_limit,
    )


def create_app(*, agent: LeadQualificationAgent | None = None, bearer_token: str | None = None) -> FastAPI:
    load_dotenv()
    settings = Settings.from_env()
    if settings.environment.lower() == "production":
        placeholders = {
            settings.api_bearer_token.get_secret_value(),
            settings.openai_api_key.get_secret_value(),
            settings.hubspot_access_token.get_secret_value(),
        }
        if "local-development-token-change-me" in placeholders or "replace_me" in placeholders:
            raise RuntimeError("API_BEARER_TOKEN, OPENAI_API_KEY, and HUBSPOT_ACCESS_TOKEN must be configured in production")
        if len(settings.api_bearer_token.get_secret_value()) < 32:
            raise RuntimeError("API_BEARER_TOKEN must contain at least 32 characters in production")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.agent = agent or build_agent(settings)
        app.state.bearer_token = bearer_token or settings.api_bearer_token.get_secret_value()
        yield
        current = app.state.agent
        for component_name in ("fetcher", "extractor", "hubspot", "storage"):
            component = getattr(current, component_name, None)
            close = getattr(component, "close", None)
            if close:
                close()

    app = FastAPI(title="Lead Qualification Agent", version="1.0.0", lifespan=lifespan)
    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id = uuid4().hex
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            logger.info(
                "http_request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
                request.method, request.url.path, status_code,
                (time.perf_counter() - started) * 1000, request_id,
            )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        agent_instance = getattr(request.app.state, "agent", None)
        if not agent_instance:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_ready")
        try:
            agent_instance.storage.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc
        return {"status": "ready"}

    @app.post("/leads/qualify", response_model=LeadResponse)
    def qualify_lead(
        payload: LeadRequest,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if not credentials or not secrets.compare_digest(credentials.credentials, request.app.state.bearer_token):
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 200:
            raise HTTPException(status_code=400, detail="valid_idempotency_key_required")
        try:
            return request.app.state.agent.qualify(payload, idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="idempotency_key_conflict") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="request_in_progress", headers={"Retry-After": "3"}) from exc
        except WebsiteFetchError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except ExtractionError as exc:
            raise HTTPException(status_code=502, detail=exc.code) from exc
        except HubSpotError as exc:
            raise HTTPException(status_code=502, detail=exc.code) from exc
        except RuntimeError as exc:
            if str(exc) == "daily_model_budget_exhausted":
                raise HTTPException(status_code=429, detail="daily_model_budget_exhausted") from exc
            raise HTTPException(status_code=502, detail="agent_execution_failed") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="agent_execution_failed") from exc

    return app


app = create_app()

