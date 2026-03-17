"""
title: Civic Funding Intelligence
author: ChangeAgent AI
description: Search federal grants (Grants.gov) and private foundations (IRS 990-PF) — discover government and philanthropic funding opportunities.
version: 0.1.0
requirements: httpx
"""

import json
import os
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

SYSTEM_PROMPT_INJECTION = """
### Civic Funding Intelligence — Usage Guide
Use this tool for grants and philanthropic funding:
- `search_grants` — Federal grant opportunities from Grants.gov (government money available to apply for)
- `search_foundations` — Private foundations that fund causes (IRS 990-PF data)
- `search_foundation_grants` — What a specific foundation has funded
- `get_grant` / `get_foundation` — Detailed views of specific records
Do NOT use this tool for federal contracts — use the Civic Procurement tool instead.
Key distinction: GRANTS fund projects/programs. CONTRACTS (procurement) pay for services.
"""


class EventEmitter:
    def __init__(self, event_emitter: Callable[[dict], Any] = None):
        self.event_emitter = event_emitter

    async def progress_update(self, description: str):
        await self.emit(description)

    async def error_update(self, description: str):
        await self.emit(description, "error", True)

    async def success_update(self, description: str):
        await self.emit(description, "success", True)

    async def emit(self, description="Unknown State", status="in_progress", done=False):
        if self.event_emitter:
            await self.event_emitter(
                {"type": "status", "data": {"status": status, "description": description, "done": done}}
            )


class Tools:
    class Valves(BaseModel):
        GOVCON_API_URL: str = Field(
            default_factory=lambda: os.getenv("GOVCON_API_URL", "https://govcon-api-production.up.railway.app"),
            description="GovCon Civic Intelligence API base URL",
        )
        GOVCON_API_KEY: str = Field(
            default_factory=lambda: os.getenv("GOVCON_API_KEY", ""),
            description="Bearer token for GovCon API authentication",
        )
        TIMEOUT: int = Field(default=30, description="HTTP request timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.valves.GOVCON_API_KEY:
            h["Authorization"] = f"Bearer {self.valves.GOVCON_API_KEY}"
        return h

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import httpx
        url = f"{self.valves.GOVCON_API_URL.rstrip('/')}/api{path}"
        cleaned = {k: v for k, v in (params or {}).items() if v is not None}
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            resp = await client.get(url, params=cleaned, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _fmt_money(val) -> str:
        if val is None:
            return "N/A"
        try:
            return f"${float(val):,.0f}"
        except (ValueError, TypeError):
            return str(val)

    # ── Tool methods ──────────────────────────────────────────────

    async def search_grants(
        self,
        query: str,
        agency: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search federal GRANT OPPORTUNITIES from Grants.gov — these are government funding opportunities
        for organizations to apply for (NOT contracts for services). Use this when the user asks about
        federal grants, government funding, grant opportunities, or "grants for [topic]." Grants fund
        projects and programs; contracts (use civic_procurement) pay for services delivered.

        :param query: Search text (e.g., "education", "STEM workforce development")
        :param agency: Agency code filter (e.g., "HHS", "DOE", "NSF")
        :param status: Grant status filter — P=Posted (open), F=Forecasted (upcoming), C=Closed, A=Archived
        :param page: Page number (default: 1)
        :return: List of federal grant opportunities with title, agency, funding amount, close date, and status.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching federal grants: {query}")

        try:
            data = await self._get("/grants", {
                "search": query,
                "agency_code": agency,
                "status": status,
                "page": page,
                "page_size": 25,
            })
        except Exception as e:
            await emitter.error_update(f"Search failed: {e}")
            return f"Error: Failed to search grants — {e}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No grants found")
            return f"No federal grant opportunities found for '{query}'."

        status_labels = {"P": "Open", "F": "Forecasted", "C": "Closed", "A": "Archived"}
        lines = [f"## Federal Grant Opportunities\n\nFound **{total}** results for \"{query}\"\n"]

        for i, grant in enumerate(items, 1):
            title = grant.get("title", "Untitled")
            agency_name = grant.get("agency_name") or grant.get("agency_code", "")
            close_date = (grant.get("close_date") or "")[:10]
            grant_status = grant.get("status", "")
            status_str = status_labels.get(grant_status, grant_status)
            opp_number = grant.get("opportunity_number", "")
            grant_id = grant.get("grant_id", "")

            lines.append(f"{i}. **{title}**")
            detail_parts = []
            if agency_name:
                detail_parts.append(f"Agency: {agency_name}")
            if opp_number:
                detail_parts.append(f"#{opp_number}")
            if status_str:
                detail_parts.append(f"Status: {status_str}")
            lines.append(f"   {' | '.join(detail_parts)}")
            if close_date:
                lines.append(f"   Closes: {close_date}")
            if grant_id:
                lines.append(f"   _ID: {grant_id} — use get_grant({grant_id}) for details_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} grant opportunities")
        return "\n".join(lines)

    async def get_grant(
        self,
        grant_id: int,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get full details for a specific federal grant opportunity from Grants.gov. Use this after
        search_grants to get the complete grant announcement including eligibility, funding details,
        and application instructions.

        :param grant_id: The grant ID (integer) from search results
        :return: Complete grant details including description, eligibility, funding range, application deadline, and agency contact.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Fetching grant {grant_id}...")

        try:
            grant = await self._get(f"/grants/{grant_id}")
        except Exception as e:
            await emitter.error_update(f"Fetch failed: {e}")
            return f"Error: Failed to fetch grant {grant_id} — {e}"

        lines = [f"## Federal Grant Detail\n"]
        lines.append(f"**{grant.get('title', 'Untitled')}**\n")

        status_labels = {"P": "Open", "F": "Forecasted", "C": "Closed", "A": "Archived"}
        fields = [
            ("Opportunity Number", grant.get("opportunity_number")),
            ("Agency", grant.get("agency_name") or grant.get("agency_code")),
            ("Status", status_labels.get(grant.get("status", ""), grant.get("status", ""))),
            ("Close Date", (grant.get("close_date") or "")[:10]),
            ("Posted Date", (grant.get("posted_date") or "")[:10]),
            ("Award Floor", self._fmt_money(grant.get("award_floor")) if grant.get("award_floor") else None),
            ("Award Ceiling", self._fmt_money(grant.get("award_ceiling")) if grant.get("award_ceiling") else None),
            ("Expected Awards", grant.get("expected_number_of_awards")),
            ("Estimated Total Funding", self._fmt_money(grant.get("estimated_total_funding")) if grant.get("estimated_total_funding") else None),
            ("Eligibility", grant.get("eligible_applicants")),
            ("Funding Instrument", grant.get("funding_instrument_type")),
            ("Category", grant.get("category_of_funding_activity")),
            ("CFDA Number", grant.get("cfda_number")),
        ]
        for label, val in fields:
            if val:
                lines.append(f"- **{label}:** {val}")

        desc = grant.get("description", "")
        if desc:
            lines.append(f"\n### Description\n\n{desc[:3000]}")
            if len(desc) > 3000:
                lines.append("\n_[Description truncated — see Grants.gov for full text]_")

        await emitter.success_update("Grant details retrieved")
        return "\n".join(lines)

    async def search_foundations(
        self,
        query: str,
        state: Optional[str] = None,
        min_giving: Optional[float] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search PRIVATE FOUNDATIONS from IRS 990-PF filings — these are philanthropic foundations that
        give money to nonprofits and causes. Use this when the user asks about private foundations,
        philanthropic funders, "foundations that fund [topic]", or foundation giving in a specific state.
        This is about the FUNDERS themselves, not their individual grants (use search_foundation_grants for that).

        :param query: Search text — foundation name (e.g., "Ford Foundation", "Gates")
        :param state: Two-letter state code filter (e.g., "NY", "CA")
        :param min_giving: Minimum total giving amount in USD (e.g., 1000000 for $1M+)
        :param page: Page number (default: 1)
        :return: List of private foundations with name, EIN, state, total assets, total giving, and NTEE classification.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching private foundations: {query}")

        try:
            data = await self._get("/foundations", {
                "search": query,
                "state": state,
                "min_giving": min_giving,
                "page": page,
                "page_size": 25,
            })
        except Exception as e:
            await emitter.error_update(f"Search failed: {e}")
            return f"Error: Failed to search foundations — {e}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No foundations found")
            return f"No private foundations found for '{query}'."

        lines = [f"## Private Foundations\n\nFound **{total}** results for \"{query}\"\n"]
        for i, fnd in enumerate(items, 1):
            name = fnd.get("name", "Unknown")
            ein = fnd.get("ein", "N/A")
            fnd_state = fnd.get("state", "")
            assets = fnd.get("total_assets")
            giving = fnd.get("total_giving")

            assets_str = self._fmt_money(assets)
            giving_str = self._fmt_money(giving)

            lines.append(f"{i}. **{name}** (EIN: {ein})")
            detail_parts = []
            if fnd_state:
                detail_parts.append(f"State: {fnd_state}")
            detail_parts.append(f"Assets: {assets_str}")
            detail_parts.append(f"Total Giving: {giving_str}")
            lines.append(f"   {' | '.join(detail_parts)}")
            lines.append(f"   _Use get_foundation(\"{ein}\") for profile, search_foundation_grants(\"{ein}\") for their grants_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} foundations")
        return "\n".join(lines)

    async def get_foundation(
        self,
        ein: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get full details for a specific private foundation by its EIN (Employer Identification Number).
        Use this after search_foundations to see a foundation's complete profile including financial
        details from their IRS 990-PF filing.

        :param ein: The foundation's EIN, with or without dash (e.g., "13-1837418" or "131837418")
        :return: Foundation profile with name, address, total assets, total giving, fiscal details, and officer information.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Fetching foundation {ein}...")

        try:
            fnd = await self._get(f"/foundations/{ein}")
        except Exception as e:
            await emitter.error_update(f"Fetch failed: {e}")
            return f"Error: Failed to fetch foundation {ein} — {e}"

        lines = [f"## Foundation Profile\n"]
        lines.append(f"**{fnd.get('name', 'Unknown')}**\n")

        fields = [
            ("EIN", fnd.get("ein")),
            ("State", fnd.get("state")),
            ("City", fnd.get("city")),
            ("NTEE Code", fnd.get("ntee_code")),
            ("Total Assets", self._fmt_money(fnd.get("total_assets")) if fnd.get("total_assets") else None),
            ("Total Giving", self._fmt_money(fnd.get("total_giving")) if fnd.get("total_giving") else None),
            ("Total Revenue", self._fmt_money(fnd.get("total_revenue")) if fnd.get("total_revenue") else None),
            ("Tax Period", fnd.get("tax_period")),
            ("Ruling Date", fnd.get("ruling_date")),
        ]
        for label, val in fields:
            if val:
                lines.append(f"- **{label}:** {val}")

        await emitter.success_update("Foundation details retrieved")
        return "\n".join(lines)

    async def search_foundation_grants(
        self,
        ein: str,
        search: Optional[str] = None,
        min_amount: Optional[float] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search grants MADE BY a specific private foundation — these are donations and grants the
        foundation has given to other organizations. Use this when the user asks "what has [foundation]
        funded?", "who does [foundation] give to?", or wants to see a foundation's grantmaking history.

        :param ein: The foundation's EIN (e.g., "13-1837418")
        :param search: Search text to filter grant recipients or purposes
        :param min_amount: Minimum grant amount in USD
        :param page: Page number (default: 1)
        :return: List of grants made by the foundation with recipient name, amount, purpose, and tax year.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Searching grants made by foundation {ein}...")

        try:
            data = await self._get(f"/foundations/{ein}/grants", {
                "search": search,
                "min_amount": min_amount,
                "page": page,
                "page_size": 25,
            })
        except Exception as e:
            await emitter.error_update(f"Search failed: {e}")
            return f"Error: Failed to search foundation grants — {e}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No foundation grants found")
            msg = f"No grants found for foundation {ein}."
            if not search:
                msg += " This foundation's grant data may not be available yet (requires Phase 2 XML extraction)."
            return msg

        lines = [f"## Grants Made by Foundation {ein}\n\nFound **{total}** grants\n"]
        for i, grant in enumerate(items, 1):
            recipient = grant.get("recipient_name", "Unknown")
            amount = grant.get("amount")
            purpose = grant.get("purpose", "")
            tax_year = grant.get("tax_year", "")

            amount_str = f"${amount:,.0f}" if amount else "N/A"
            lines.append(f"{i}. **{recipient}** — {amount_str}")
            detail_parts = []
            if purpose:
                detail_parts.append(purpose[:150])
            if tax_year:
                detail_parts.append(f"Tax year: {tax_year}")
            if detail_parts:
                lines.append(f"   {' | '.join(detail_parts)}")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} grants from this foundation")
        return "\n".join(lines)
