from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from rss_surveillance_agent.api import create_app
from rss_surveillance_agent.hubspot import HubSpotClient, HubSpotError
from rss_surveillance_agent.lead_agent import LeadQualificationAgent
from rss_surveillance_agent.lead_config import ScorePolicy
from rss_surveillance_agent.lead_logic import score_facts
from rss_surveillance_agent.lead_models import (
    CompanyFacts,
    Evidence,
    LeadRequest,
    Qualification,
)
from rss_surveillance_agent.lead_storage import LeadStorage
from rss_surveillance_agent.website import (
    WebsiteContent,
    WebsiteFetcher,
    WebsiteFetchError,
)

TEXT = (
    "Example Robotics builds warehouse automation systems for logistics operators. "
    "The team has 100 employees. It builds warehouse robotics systems. "
    "The company launched a new robotics platform this year."
)
URL = "https://example.com/"


def _facts(confidence: float = 0.9) -> CompanyFacts:
    return CompanyFacts(
        company_summary="Example Robotics builds warehouse automation systems.",
        industry="warehouse logistics",
        employee_count=100,
        relevant_product=True,
        product_summary="Builds warehouse robotics systems.",
        recent_growth_signals=["Launched a robotics platform this year."],
        evidence=[
            Evidence(field="company_summary", fact="Example Robotics builds warehouse automation systems", quote="Example Robotics builds warehouse automation systems for logistics operators", source_url=URL),
            Evidence(field="industry", fact="warehouse logistics", quote="warehouse automation systems for logistics operators", source_url=URL),
            Evidence(field="employee_count", fact="The team has 100 employees", quote="The team has 100 employees", source_url=URL),
            Evidence(field="relevant_product", fact="Builds warehouse robotics systems", quote="It builds warehouse robotics systems", source_url=URL),
            Evidence(field="recent_growth_signal", fact="Launched a new robotics platform this year", quote="The company launched a new robotics platform this year", source_url=URL),
        ],
        confidence=confidence,
    )


def test_deterministic_scoring_and_quote_grounding():
    request = LeadRequest(contact_id="123", company_name="Example Robotics", website=URL)
    website = WebsiteContent(URL, "Example", TEXT)
    facts = _facts()
    result = score_facts(request, facts, ScorePolicy(), website)
    assert result.report.score == 100
    assert result.report.qualification == Qualification.HOT
    assert result.requires_review is False

    facts.employee_count = 500  # fabricated count contradicts the cited page
    result = score_facts(request, facts, ScorePolicy(), website)
    assert result.report.score == 80  # no employee-size points without matching evidence
    assert result.requires_review is True


def test_low_confidence_requires_review_without_crm_write():
    req = LeadRequest(contact_id="1", company_name="Example", website=URL)
    result = score_facts(req, _facts(confidence=0.2), ScorePolicy(), WebsiteContent(URL, "Example", TEXT))
    assert result.requires_review
    assert "Manual review" in result.report.recommended_action


def test_storage_idempotency_terminal_replay_and_daily_budget(tmp_path):
    store = LeadStorage(f"sqlite+pysqlite:///{(tmp_path / 'state.sqlite3').as_posix()}")
    payload = {"contact_id": "123", "company_name": "Example", "website": URL}
    store.claim("request-0002", payload)
    report = {"score": 45, "qualification": "warm"}
    store.save_report("request-0002", report, status="review_required")
    replay = store.claim("request-0002", payload)
    assert replay["decision"] == "replay"
    assert replay["report_json"]
    assert store.claim("request-0002", {**payload, "company_name": "Different"})["decision"] == "key_conflict"
    assert store.reserve_model_call(1)
    assert not store.reserve_model_call(1)
    store.close()


def test_hubspot_contact_verification_and_note_association():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "123"})
        return httpx.Response(201, json={"id": "note-456"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    crm = HubSpotClient("test-token", client=client)
    crm.verify_contact("123")
    req = LeadRequest(contact_id="123", company_name="Example", website=URL)
    note_id = crm.create_note(req, score_facts(req, _facts(), ScorePolicy(), WebsiteContent(URL, "", TEXT)).report)
    assert note_id == "note-456"
    payload = __import__("json").loads(seen[1].content)
    assert payload["associations"][0]["to"]["id"] == "123"
    assert payload["associations"][0]["types"][0]["associationTypeId"] == 202
    assert "hs_note_body" in payload["properties"]
    assert seen[0].url.path.endswith("/crm/objects/2026-09/contacts/123")


def test_hubspot_contact_mismatch_fails_closed():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "999"})))
    with pytest.raises(Exception, match="hubspot_contact_mismatch"):
        HubSpotClient("token", client=client).verify_contact("123")


def test_website_fetcher_rejects_private_address_before_network():
    fetcher = WebsiteFetcher(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))))
    with pytest.raises(WebsiteFetchError, match="website_address_blocked"):
        fetcher.fetch("http://127.0.0.1/")
    with pytest.raises(WebsiteFetchError, match="invalid_website_url"):
        fetcher.fetch("https://example.com:99999/")


class FakeStorage:
    def ping(self):
        return True


class FakeAgent:
    storage = FakeStorage()

    def __init__(self):
        self.calls = 0

    def qualify(self, request, idempotency_key):
        self.calls += 1
        return {"request_id": "r-1", "status": "review_required", "report": {
            "company_summary": "Example", "score": 10, "qualification": "cold",
            "opportunity_signals": [], "risk_flags": ["insufficient data"], "evidence": [],
            "recommended_action": "Manual review.", "confidence": 0.2,
        }}


def test_api_auth_validation_and_business_response():
    agent = FakeAgent()
    app = create_app(agent=agent, bearer_token="test-secret")
    with TestClient(app) as client:
        body = {"contact_id": "123", "company_name": "Example", "website": URL}
        unauthorized = client.post("/leads/qualify", json=body, headers={"Idempotency-Key": "request-0001"})
        assert unauthorized.status_code == 401
        invalid = client.post("/leads/qualify", json=body, headers={
            "Authorization": "Bearer test-secret", "Idempotency-Key": "x",
        })
        assert invalid.status_code == 400
        response = client.post("/leads/qualify", json=body, headers={
            "Authorization": "Bearer test-secret", "Idempotency-Key": "request-0001",
        })
        assert response.status_code == 200
        assert response.json()["status"] == "review_required"
        assert response.json()["report"]["score"] == 10
        assert agent.calls == 1


def test_langgraph_end_to_end_and_idempotent_crm_write(tmp_path):
    class Fetcher:
        def fetch(self, url):
            return WebsiteContent(URL, "Example", TEXT)

    class Extractor:
        def extract(self, request, website):
            return _facts()

    class CRM:
        calls = 0

        def verify_contact(self, contact_id):
            assert contact_id == "123"

        def create_note(self, request, report):
            self.calls += 1
            return "note-1"

    storage = LeadStorage(f"sqlite+pysqlite:///{(tmp_path / 'graph.sqlite3').as_posix()}")
    crm = CRM()
    graph_agent = LeadQualificationAgent(
        fetcher=Fetcher(), extractor=Extractor(), hubspot=crm, storage=storage,
        policy=ScorePolicy(), daily_model_call_limit=2,
    )
    request = LeadRequest(contact_id="123", company_name="Example Robotics", website=URL)
    first = graph_agent.qualify(request, "idempotency-demo-01")
    replay = graph_agent.qualify(request, "idempotency-demo-01")
    assert first["status"] == "completed"
    assert first["hubspot_note_id"] == "note-1"
    assert replay["replayed"] is True
    assert replay["request_id"] == first["request_id"]
    assert crm.calls == 1
    storage.close()


def test_unknown_hubspot_write_outcome_is_not_retried(tmp_path):
    class Fetcher:
        def fetch(self, url):
            return WebsiteContent(URL, "Example", TEXT)

    class Extractor:
        def extract(self, request, website):
            return _facts()

    class CRM:
        calls = 0

        def verify_contact(self, contact_id):
            pass

        def create_note(self, request, report):
            self.calls += 1
            raise HubSpotError("hubspot_note_outcome_unknown")

    storage = LeadStorage(f"sqlite+pysqlite:///{(tmp_path / 'ambiguous.sqlite3').as_posix()}")
    crm = CRM()
    agent = LeadQualificationAgent(fetcher=Fetcher(), extractor=Extractor(), hubspot=crm,
                                   storage=storage, policy=ScorePolicy(), daily_model_call_limit=2)
    request = LeadRequest(contact_id="123", company_name="Example Robotics", website=URL)
    with pytest.raises(HubSpotError):
        agent.qualify(request, "ambiguous-write-01")
    replay = agent.qualify(request, "ambiguous-write-01")
    assert replay["status"] == "review_required"
    assert replay["error_code"] == "hubspot_note_outcome_unknown"
    assert crm.calls == 1
    storage.close()

