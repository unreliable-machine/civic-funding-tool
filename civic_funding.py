"""
title: Civic Funding Intelligence
author: ChangeAgent AI
description: Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.
version: 0.2.0
requirements: httpx
"""

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

SYSTEM_PROMPT_INJECTION = """
### Civic Funding Intelligence — Usage Guide
Use this tool for grants and philanthropic funding:
- `search_grants` — Federal grant opportunities from Grants.gov (government money available to apply for)
- `search_state_grants` — State-level grant opportunities across all 50 states (128 sources)
- `search_state_awards` — Who received state grant funding
- `get_state_grant` — Full detail for a specific state grant
- `search_foundations` — Private foundations that fund causes (IRS 990-PF data)
- `search_foundation_grants` — What a specific foundation has funded
- `get_grant` / `get_foundation` — Detailed views of specific records
Do NOT use this tool for federal contracts — use the Civic Procurement tool instead.
Key distinction: GRANTS fund projects/programs. CONTRACTS (procurement) pay for services.
State vs federal: If the user mentions a specific US state or "state grants", use search_state_grants. For federal/national grants, use search_grants.
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
            description="GovCon Civic Intelligence API base URL (federal grants, foundations)",
        )
        CIVIC_FUNDING_URL: str = Field(
            default_factory=lambda: os.getenv(
                "CIVIC_FUNDING_URL",
                "https://civic-funding-production.up.railway.app",
            ),
            description="Civic Funding API base URL (state grants, state awards)",
        )
        GOVCON_API_KEY: str = Field(
            default_factory=lambda: os.getenv("GOVCON_API_KEY", ""),
            description="Bearer token for API authentication",
        )
        TIMEOUT: int = Field(default=30, description="HTTP request timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.valves.GOVCON_API_KEY:
            h["Authorization"] = f"Bearer {self.valves.GOVCON_API_KEY}"
        return h

    # ── Anti-fragile HTTP helpers ──────────────────────────────────

    async def _get_govcon(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """GET from govcon-api. Returns (data, None) or (None, error)."""
        return await self._get_with_retry(
            f"{self.valves.GOVCON_API_URL.rstrip('/')}/api{path}", params
        )

    async def _get_funding(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """GET from civic-funding. Returns (data, None) or (None, error)."""
        return await self._get_with_retry(
            f"{self.valves.CIVIC_FUNDING_URL.rstrip('/')}/api{path}", params
        )

    async def _get_with_retry(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Anti-fragile GET: 2 attempts on 5xx/connection errors. Never raises."""
        import httpx

        cleaned = {k: v for k, v in (params or {}).items() if v is not None}
        t = self.valves.TIMEOUT
        backoffs = [1, 3]

        last_error = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=t) as client:
                    resp = await client.get(url, params=cleaned, headers=self._headers())
                    if resp.status_code >= 500 and attempt < 1:
                        last_error = f"Server error ({resp.status_code})"
                        await asyncio.sleep(backoffs[attempt])
                        continue
                    if resp.status_code == 401:
                        return None, "Authentication failed — check API key configuration"
                    if resp.status_code == 404:
                        return None, "Resource not found"
                    if resp.status_code >= 400:
                        return None, f"Request error ({resp.status_code})"
                    return resp.json(), None
            except httpx.TimeoutException:
                last_error = f"Request timed out after {t}s"
                if attempt < 1:
                    await asyncio.sleep(backoffs[attempt])
                    continue
            except httpx.ConnectError:
                last_error = "Service unavailable — connection failed"
                if attempt < 1:
                    await asyncio.sleep(backoffs[attempt])
                    continue
            except Exception as e:
                return None, f"Unexpected error: {str(e)[:200]}"

        return None, last_error

    @staticmethod
    def _fmt_money(val) -> str:
        if val is None:
            return "N/A"
        try:
            return f"${float(val):,.0f}"
        except (ValueError, TypeError):
            return str(val)

    # ── Federal grants (govcon-api) ────────────────────────────────

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

        data, error = await self._get_govcon("/grants", {
            "search": query,
            "agency_code": agency,
            "status": status,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search grants — {error}"

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

        data, error = await self._get_govcon(f"/grants/{grant_id}")
        if error:
            await emitter.error_update(f"Fetch failed: {error}")
            return f"Error: Failed to fetch grant {grant_id} — {error}"

        grant = data
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

    # ── Foundations (govcon-api) ────────────────────────────────────

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

        data, error = await self._get_govcon("/foundations", {
            "search": query,
            "state": state,
            "min_giving": min_giving,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search foundations — {error}"

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

        data, error = await self._get_govcon(f"/foundations/{ein}")
        if error:
            await emitter.error_update(f"Fetch failed: {error}")
            return f"Error: Failed to fetch foundation {ein} — {error}"

        fnd = data
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

        data, error = await self._get_govcon(f"/foundations/{ein}/grants", {
            "search": search,
            "min_amount": min_amount,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search foundation grants — {error}"

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

    # ── State grants & awards (civic-funding) ──────────────────────

    async def search_state_grants(
        self,
        query: str = "",
        state: Optional[str] = None,
        agency: Optional[str] = None,
        status: Optional[str] = None,
        close_date_after: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search STATE-LEVEL grant opportunities across all 50 US states — sourced from 128 scrapers
        covering state agencies, IntelliGrants, WebGrants, eCivis, Socrata, and CKAN portals. Use this
        when the user asks about state grants, grants in a specific US state, or non-federal government
        funding. For federal grants, use search_grants instead.

        :param query: Search text (e.g., "housing", "workforce development", "clean energy")
        :param state: Two-letter state code filter (e.g., "CA", "NY", "TX")
        :param agency: State agency name filter — partial match (e.g., "Department of Education")
        :param status: Grant status filter — open, closed, forecasted, or awarded
        :param close_date_after: Only grants closing on or after this date (YYYY-MM-DD)
        :param page: Page number (default: 1)
        :return: List of state grant opportunities with title, state, agency, amount, deadline, and source portal.
        """
        emitter = EventEmitter(__event_emitter__)
        desc = f"state grants: {query}" if query else "state grants"
        if state:
            desc += f" in {state.upper()}"
        await emitter.progress_update(f"Searching {desc}")

        data, error = await self._get_funding("/state-grants", {
            "search": query,
            "state_code": state.upper() if state else None,
            "agency_name": agency,
            "status": status,
            "close_date_after": close_date_after,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search state grants — {error}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No state grants found")
            parts = []
            if query:
                parts.append(f"'{query}'")
            if state:
                parts.append(f"in {state.upper()}")
            return f"No state grant opportunities found {' '.join(parts)}." if parts else "No state grant opportunities found."

        header = "## State Grant Opportunities\n\n"
        header += f"Found **{total}** results"
        if query:
            header += f" for \"{query}\""
        if state:
            header += f" in {state.upper()}"
        header += "\n"
        lines = [header]

        status_labels = {"open": "Open", "closed": "Closed", "forecasted": "Forecasted", "awarded": "Awarded"}
        sources = set()

        for i, grant in enumerate(items, 1):
            title = grant.get("title", "Untitled")
            grant_state = grant.get("state_code", "")
            agency_name = grant.get("agency_name", "")
            grant_status = grant.get("status") or ""
            status_str = status_labels.get(grant_status.lower(), grant_status) if grant_status else ""
            close_date = (grant.get("close_date") or "")[:10]
            amount_min = grant.get("award_floor") or grant.get("amount_min")
            amount_max = grant.get("award_ceiling") or grant.get("amount_max")
            grant_id = grant.get("state_grant_id", "")
            source_url = grant.get("source_url", "")
            source_name = grant.get("source_name", "")

            if source_name:
                sources.add(source_name)

            lines.append(f"{i}. **{title}**")
            detail_parts = []
            if grant_state:
                detail_parts.append(f"State: {grant_state}")
            if agency_name:
                detail_parts.append(f"Agency: {agency_name}")
            if status_str:
                detail_parts.append(f"Status: {status_str}")
            lines.append(f"   {' | '.join(detail_parts)}")

            if amount_min or amount_max:
                if amount_min and amount_max and amount_min != amount_max:
                    lines.append(f"   Funding: {self._fmt_money(amount_min)} – {self._fmt_money(amount_max)}")
                elif amount_max:
                    lines.append(f"   Funding: up to {self._fmt_money(amount_max)}")
                elif amount_min:
                    lines.append(f"   Funding: from {self._fmt_money(amount_min)}")

            if close_date:
                lines.append(f"   Closes: {close_date}")

            if source_url:
                lines.append(f"   [View on state portal]({source_url})")

            if grant_id:
                lines.append(f"   _Use get_state_grant(\"{grant_id}\") for full details_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._\n")

        if sources:
            lines.append(f"**Sources:** {', '.join(sorted(sources))}")

        await emitter.success_update(f"Found {total} state grant opportunities")
        return "\n".join(lines)

    async def get_state_grant(
        self,
        state_grant_id: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Get full details for a specific state grant opportunity. Use this after search_state_grants
        to see the complete grant announcement including eligibility, funding details, deadlines,
        and application instructions.

        :param state_grant_id: The state grant ID (string) from search results
        :return: Complete state grant details including description, eligibility, funding range, deadline, agency, and source link.
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update(f"Fetching state grant {state_grant_id}...")

        data, error = await self._get_funding(f"/state-grants/{state_grant_id}")
        if error:
            await emitter.error_update(f"Fetch failed: {error}")
            return f"Error: Failed to fetch state grant {state_grant_id} — {error}"

        grant = data
        lines = [f"## State Grant Detail\n"]
        lines.append(f"**{grant.get('title', 'Untitled')}**\n")

        status_labels = {"open": "Open", "closed": "Closed", "forecasted": "Forecasted", "awarded": "Awarded"}
        grant_status = grant.get("status") or ""

        fields = [
            ("State", grant.get("state_code")),
            ("Agency", grant.get("agency_name")),
            ("Status", status_labels.get(grant_status.lower(), grant_status) if grant_status else None),
            ("Close Date", (grant.get("close_date") or "")[:10]),
            ("Posted Date", (grant.get("posted_date") or "")[:10]),
            ("Award Floor", self._fmt_money(grant.get("award_floor")) if grant.get("award_floor") else None),
            ("Award Ceiling", self._fmt_money(grant.get("award_ceiling")) if grant.get("award_ceiling") else None),
            ("Total Funding", self._fmt_money(grant.get("total_funding")) if grant.get("total_funding") else None),
            ("Eligibility", grant.get("eligibility")),
            ("Category", ", ".join(grant["categories"]) if grant.get("categories") else None),
            ("Source", grant.get("source")),
        ]
        for label, val in fields:
            if val:
                lines.append(f"- **{label}:** {val}")

        source_url = grant.get("source_url", "")
        if source_url:
            lines.append(f"- **Portal Link:** [{source_url}]({source_url})")

        desc = grant.get("description", "")
        if desc:
            lines.append(f"\n### Description\n\n{desc[:3000]}")
            if len(desc) > 3000:
                lines.append("\n_[Description truncated — see state portal for full text]_")

        await emitter.success_update("State grant details retrieved")
        return "\n".join(lines)

    async def search_state_awards(
        self,
        query: str = "",
        state: Optional[str] = None,
        agency: Optional[str] = None,
        recipient: Optional[str] = None,
        fiscal_year: Optional[int] = None,
        min_amount: Optional[float] = None,
        max_amount: Optional[float] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search STATE GRANT AWARD RECIPIENTS — find out who received state grant funding, how much,
        and from which agency. Use this when the user asks "who got state grants for [topic]",
        "state funding recipients in [state]", or wants to see actual disbursement data.

        :param query: Search text (e.g., "education", "housing assistance")
        :param state: Two-letter state code filter (e.g., "CA", "NY")
        :param agency: State agency name filter — partial match
        :param recipient: Recipient organization name filter — partial match
        :param fiscal_year: Fiscal year filter (e.g., 2025)
        :param min_amount: Minimum award amount in USD
        :param max_amount: Maximum award amount in USD
        :param page: Page number (default: 1)
        :return: List of state grant awards with recipient name, amount, agency, state, and fiscal year.
        """
        emitter = EventEmitter(__event_emitter__)
        desc = f"state awards: {query}" if query else "state awards"
        if state:
            desc += f" in {state.upper()}"
        await emitter.progress_update(f"Searching {desc}")

        data, error = await self._get_funding("/state-awards", {
            "search": query,
            "state_code": state.upper() if state else None,
            "agency_name": agency,
            "recipient_name": recipient,
            "fiscal_year": fiscal_year,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search state awards — {error}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No state awards found")
            parts = []
            if query:
                parts.append(f"'{query}'")
            if state:
                parts.append(f"in {state.upper()}")
            return f"No state grant awards found {' '.join(parts)}." if parts else "No state grant awards found."

        header = "## State Grant Awards\n\n"
        header += f"Found **{total}** results"
        if query:
            header += f" for \"{query}\""
        if state:
            header += f" in {state.upper()}"
        header += "\n"
        lines = [header]

        sources = set()

        for i, award in enumerate(items, 1):
            recipient_name = award.get("recipient_name", "Unknown")
            amount = award.get("award_amount") or award.get("amount")
            agency_name = award.get("agency_name", "")
            award_state = award.get("state_code", "")
            year = award.get("fiscal_year", "")
            program = award.get("program_name", "")
            source_name = award.get("source_name", "")
            source_url = award.get("source_url", "")

            if source_name:
                sources.add(source_name)

            amount_str = self._fmt_money(amount) if amount else "N/A"
            lines.append(f"{i}. **{recipient_name}** — {amount_str}")
            detail_parts = []
            if award_state:
                detail_parts.append(f"State: {award_state}")
            if agency_name:
                detail_parts.append(f"Agency: {agency_name}")
            if year:
                detail_parts.append(f"FY{year}")
            lines.append(f"   {' | '.join(detail_parts)}")
            if program:
                lines.append(f"   Program: {program}")
            if source_url:
                lines.append(f"   [View source]({source_url})")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._\n")

        if sources:
            lines.append(f"**Sources:** {', '.join(sorted(sources))}")

        await emitter.success_update(f"Found {total} state grant awards")
        return "\n".join(lines)
