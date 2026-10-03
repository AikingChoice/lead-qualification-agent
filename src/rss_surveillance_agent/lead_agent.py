from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .hubspot import HubSpotClient
from .lead_config import ScorePolicy
from .lead_logic import OpenAICompanyFactExtractor, score_facts
from .lead_models import LeadRequest, LeadStatus, QualificationReport
from .lead_storage import LeadStorage
from .website import WebsiteFetcher


class AgentState(TypedDict, total=False):
    request: LeadRequest
    idempotency_key: str
    website: Any
    facts: Any
    report: QualificationReport
    review: bool
    review_reason: str | None
    note_id: str | None
    error_code: str | None


class LeadQualificationAgent:
    """Small explicit LangGraph workflow; deterministic code owns scoring and writes."""

    def __init__(self, *, fetcher: WebsiteFetcher, extractor: OpenAICompanyFactExtractor,
                 hubspot: HubSpotClient, storage: LeadStorage, policy: ScorePolicy,
                 daily_model_call_limit: int):
        self.fetcher = fetcher
        self.extractor = extractor
        self.hubspot = hubspot
        self.storage = storage
        self.policy = policy
        self.daily_model_call_limit = daily_model_call_limit
        graph = StateGraph(AgentState)
        graph.add_node("fetch_website", self._fetch_website)
        graph.add_node("extract_facts", self._extract_facts)
        graph.add_node("score_and_gate", self._score_and_gate)
        graph.add_node("write_hubspot_note", self._write_note)
        graph.add_edge(START, "fetch_website")
        graph.add_edge("fetch_website", "extract_facts")
        graph.add_edge("extract_facts", "score_and_gate")
        graph.add_conditional_edges("score_and_gate", self._route_after_score, {
            "review": END, "write": "write_hubspot_note",
        })
        graph.add_edge("write_hubspot_note", END)
        self.graph = graph.compile()

    def _fetch_website(self, state: AgentState) -> dict[str, Any]:
        return {"website": self.fetcher.fetch(str(state["request"].website))}

    def _extract_facts(self, state: AgentState) -> dict[str, Any]:
        if not self.storage.reserve_model_call(self.daily_model_call_limit):
            raise RuntimeError("daily_model_budget_exhausted")
        return {"facts": self.extractor.extract(state["request"], state["website"])}

    def _score_and_gate(self, state: AgentState) -> dict[str, Any]:
        scored = score_facts(state["request"], state["facts"], self.policy, state["website"])
        status = LeadStatus.REVIEW_REQUIRED.value if scored.requires_review else "processing"
        self.storage.save_report(state["idempotency_key"], scored.report.model_dump(mode="json"), status=status,
                                 error_code=scored.review_reason)
        return {"report": scored.report, "review": scored.requires_review, "review_reason": scored.review_reason}

    @staticmethod
    def _route_after_score(state: AgentState) -> str:
        return "review" if state.get("review") else "write"

    def _write_note(self, state: AgentState) -> dict[str, Any]:
        request: LeadRequest = state["request"]
        self.hubspot.verify_contact(request.contact_id)
        self.storage.mark_hubspot_write_started(state["idempotency_key"])
        note_id = self.hubspot.create_note(request, state["report"])
        self.storage.complete(state["idempotency_key"], note_id)
        return {"note_id": note_id}

    def qualify(self, request: LeadRequest, idempotency_key: str) -> dict[str, Any]:
        claimed = self.storage.claim(idempotency_key, request.model_dump(mode="json"))
        decision = claimed["decision"]
        if decision == "key_conflict":
            raise ValueError("idempotency_key_reused_with_different_request")
        if decision == "in_progress":
            raise TimeoutError("request_in_progress")
        if decision == "replay":
            report = QualificationReport.model_validate_json(claimed["report_json"]) if claimed.get("report_json") else None
            if report is None:
                raise RuntimeError("stored_report_missing")
            return {
                "request_id": claimed["request_id"], "status": claimed["status"], "report": report,
                "hubspot_note_id": claimed.get("note_id"), "replayed": True,
                "error_code": claimed.get("error_code"),
            }
        request_id = claimed["request_id"]
        try:
            state = self.graph.invoke({"request": request, "idempotency_key": idempotency_key})
        except Exception as exc:
            code = getattr(exc, "code", "agent_execution_failed")
            if str(exc) == "daily_model_budget_exhausted":
                code = "daily_model_budget_exhausted"
            self.storage.fail(
                idempotency_key,
                code,
                review=code in {"hubspot_note_outcome_unknown", "hubspot_note_rejected"},
            )
            raise
        status = LeadStatus.REVIEW_REQUIRED if state.get("review") else LeadStatus.COMPLETED
        return {
            "request_id": request_id, "status": status, "report": state["report"],
            "hubspot_note_id": state.get("note_id"), "replayed": False,
            "error_code": state.get("review_reason"),
        }

