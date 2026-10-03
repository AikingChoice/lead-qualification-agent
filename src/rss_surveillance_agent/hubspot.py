from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from .lead_models import LeadRequest, QualificationReport


class HubSpotError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class HubSpotClient:
    def __init__(self, access_token: str, api_version: str = "2026-09", *, client: httpx.Client | None = None):
        self.base_url = f"https://api.hubapi.com/crm/objects/{api_version}"
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self.headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    def verify_contact(self, contact_id: str) -> None:
        try:
            response = self.client.get(f"{self.base_url}/contacts/{contact_id}", headers=self.headers)
        except httpx.HTTPError as exc:
            raise HubSpotError("hubspot_unavailable") from exc
        if response.status_code == 404:
            raise HubSpotError("hubspot_contact_not_found")
        if response.status_code in {401, 403}:
            raise HubSpotError("hubspot_auth_or_scope_error")
        if response.status_code >= 400:
            raise HubSpotError("hubspot_contact_lookup_failed")
        try:
            data = response.json()
        except ValueError as exc:
            raise HubSpotError("hubspot_invalid_response") from exc
        if str(data.get("id")) != contact_id:
            raise HubSpotError("hubspot_contact_mismatch")

    def create_note(self, request: LeadRequest, report: QualificationReport) -> str:
        body = "Lead qualification report\n\n" + json.dumps(
            report.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        payload = {
            "properties": {
                "hs_timestamp": str(int(datetime.now(UTC).timestamp() * 1000)),
                "hs_note_body": body[:60_000],
            },
            "associations": [{
                "to": {"id": request.contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }],
        }
        try:
            response = self.client.post(f"{self.base_url}/notes", headers=self.headers, json=payload)
        except httpx.HTTPError as exc:
            raise HubSpotError("hubspot_note_outcome_unknown") from exc
        if response.status_code in {401, 403}:
            raise HubSpotError("hubspot_auth_or_scope_error")
        if response.status_code >= 500 or response.status_code == 429:
            raise HubSpotError("hubspot_note_outcome_unknown")
        if response.status_code >= 400:
            raise HubSpotError("hubspot_note_rejected")
        try:
            note_id = str(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HubSpotError("hubspot_invalid_response") from exc
        if not note_id:
            raise HubSpotError("hubspot_invalid_response")
        return note_id

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

