"""
title: Civic Funding Intelligence
author: Sev
author_url: https://thechange.ai
id: civic_funding_intelligence
description: Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), top funders by state, and active RFPs with deadlines.
required_open_webui_version: 0.4.0
requirements: httpx, pydantic
version: 2.3.0
license: MIT
"""

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

SYSTEM_PROMPT_INJECTION = """You have access to the Civic Funding Intelligence tool — a grants and philanthropic funding research toolkit.

START HERE:
- funding_discover: THE DEFAULT. Takes a description of the org + optional state, searches ALL sources automatically (foundation grants by purpose, federal grants, state grants). Use this FIRST for any funding discovery question. ONE call replaces multiple searches.

FOLLOW-UP FUNCTIONS (use AFTER funding_discover, for drilling into specific results):
- funding_get_grant: Get full details for a specific federal grant by ID
- funding_get_foundation: Get full profile for a specific foundation by EIN
- funding_search_foundation_grants: See what a SPECIFIC foundation has funded (requires EIN)
- funding_get_state_grant: Get full details for a specific state grant
- funding_search_rfps: Search for currently open foundation RFPs with deadlines and application links
- funding_search_grants_by_purpose: Search foundation grants by issue area (funding_discover calls this internally — use directly only for refined follow-up searches)
- funding_search_grants: Search federal grants (funding_discover calls this internally)
- funding_search_state_grants: Search state grants (funding_discover calls this internally)
- funding_search_foundations: Search foundations by NAME (only when user asks about a specific foundation)
- funding_search_state_awards: Search state grant recipients

ROUTING:
- "What grants should I apply to?" → funding_discover (ONE call)
- "I'm a nonprofit in NM focused on X" → funding_discover(description="nonprofit focused on X", state="NM")
- "What foundations fund education?" → funding_discover(description="education")
- "Tell me more about the Ford Foundation" → funding_get_foundation (follow-up)
- "What has Kellogg funded in NM?" → funding_search_foundation_grants (follow-up, needs EIN)

DO NOT guess foundation names and search them one by one. funding_discover handles discovery.

KEY DISTINCTION: GRANTS fund projects/programs. CONTRACTS (use GovCon Intelligence tool) pay for services.

ANTI-HALLUCINATION (CRITICAL):
- ONLY present data returned by tool calls. NEVER invent grant amounts, deadlines, agency names, EINs, or foundation details.
- If a tool returns no results, say "no results found" — do NOT fill in with guesses or general knowledge about grants that might exist.
- NEVER fabricate URLs. Only include URLs returned by the tool.
- When summarizing results, use EXACT values from tool output. Do not round or approximate.
- If the user asks about something not in the results, say "this was not in the search results."
- NEVER say "based on my knowledge" about funding data. Either you have it from a tool call or you don't.
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

    # ── Discovery (primary entry point) ─────────────────────────────

    async def funding_discover(
        self,
        description: str,
        state: Optional[str] = None,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool.</TOOL INFO>

        THE PRIMARY FUNCTION for grant discovery. Takes a description of who the user is and what they do, then searches across ALL funding sources automatically — private foundation grants (by purpose), federal grants, and state grants. Returns a consolidated funding landscape. Use this FIRST for any "what grants should I apply to?" or "what funding is available for [cause]?" question.</Function Definition>

        :param description: Describe the organization and its work (e.g., "NM communications nonprofit focused on civic engagement")
        :param state: Two-letter state code (e.g., "NM") — filters foundation grants by recipient state and state grants by state
        :return: Consolidated funding landscape with foundation grants, federal opportunities, and state grants.
        """
        emitter = EventEmitter(__event_emitter__)

        # Extract 2-3 short search phrases from the description.
        # Strategy: pull recognizable issue-area bigrams/trigrams, not strip words.
        desc_lower = description.lower()

        # Known issue-area phrases to detect (order = priority)
        known_phrases = [
            "civic engagement", "community organizing", "voter engagement",
            "voting rights", "democracy", "social justice", "racial justice",
            "climate justice", "environmental justice", "health equity",
            "criminal justice", "immigration", "housing", "education",
            "workforce development", "economic justice", "reproductive rights",
            "gun violence", "public health", "child welfare", "early childhood",
            "narrative change", "media", "communications", "advocacy",
            "organizing", "civil rights", "human rights", "LGBTQ",
            "indigenous", "tribal", "rural", "urban",
        ]
        # Find which known phrases appear in the description
        matched = [p for p in known_phrases if p in desc_lower]

        # If no known phrases matched, extract content words as fallback
        if not matched:
            filler = {"i'm", "im", "a", "an", "the", "we", "are", "our", "is", "in", "of",
                      "and", "for", "to", "that", "with", "on", "at", "by", "my", "this",
                      "nonprofit", "non-profit", "501c3", "501c4", "501(c)(3)", "501(c)(4)",
                      "organization", "org", "small", "large", "new", "what", "grants",
                      "should", "apply", "can", "get", "looking", "funding", "money",
                      "focused", "based", "work", "do", "does", "about", "i", "me",
                      "progress", "now"}  # org name words likely to pollute
            words = description.lower().split()
            content = [w.strip(",.?!-()") for w in words if w.strip(",.?!-()") not in filler and len(w) > 2]
            # Take up to 3 content words as a single query
            if content:
                matched = [" ".join(content[:4])]

        # Deduplicate and limit to 3 search phrases
        search_queries = list(dict.fromkeys(matched))[:3]
        if not search_queries:
            search_queries = [description[:80]]  # absolute fallback

        state_label = f" in {state.upper()}" if state else ""
        sections = []
        all_grants = {}  # dedupe by grant_key across queries

        # ── 0. Top funders in this state (regardless of purpose match) ──
        if state:
            await emitter.progress_update(f"Finding top funders in {state.upper()}...")
            data, error = await self._get_govcon("/foundations/top-by-state", {
                "state": state.upper(),
                "limit": 15,
            })
            if not error and data and data.get("results"):
                lines = [f"## Top Funders in {state.upper()} (by total giving to state)\n"]
                lines.append("These foundations give the most to organizations in your state — worth researching even if their stated purpose doesn't match your exact work.\n")
                for i, f in enumerate(data["results"][:10], 1):
                    name = f.get("foundation_name", "Unknown")
                    total = self._fmt_money(f.get("total_to_state"))
                    count = f.get("grant_count", 0)
                    years = f.get("years_active", "")
                    lines.append(f"{i}. **{name}** — {total} across {count} grants")
                    detail = []
                    if years:
                        detail.append(f"Years: {years}")
                    purposes = f.get("sample_purposes", [])
                    if purposes:
                        detail.append(f"e.g., {purposes[0][:80]}")
                    if detail:
                        lines.append(f"   {' | '.join(detail)}")
                    fein = f.get("foundation_ein", "")
                    if fein:
                        pp_ein = fein.replace("-", "")
                        lines.append(f"   [ProPublica Profile](https://projects.propublica.org/nonprofits/organizations/{pp_ein}) | _Use funding_get_foundation({fein}) for full details_")
                    lines.append("")
                sections.append("\n".join(lines))

        # ── 1. Foundation grants by purpose (with state filter) ──
        if state:
            for sq in search_queries:
                await emitter.progress_update(f"Searching foundation grants for '{sq}'{state_label}...")
                data, error = await self._get_govcon("/foundations/grants/search", {
                    "search": sq,
                    "state": state.upper(),
                    "page_size": 15,
                })
                if not error and data:
                    for g in data.get("results", []):
                        gk = g.get("grant_key", id(g))
                        if gk not in all_grants:
                            all_grants[gk] = g

            items = sorted(all_grants.values(), key=lambda g: float(g.get("amount") or 0), reverse=True)
            if items:
                total = len(items)
                lines = [f"## Priority Matches: Foundations That Fund Your Kind of Work in {state.upper()}\n"]
                lines.append(f"Found **{total}** grants from private foundations to {state.upper()} organizations doing similar work. These are your strongest leads.\n")
                foundations_seen = {}
                for i, g in enumerate(items[:10], 1):
                    fname = g.get("foundation_name", "Unknown")
                    fein = g.get("foundation_ein", "")
                    recipient = g.get("recipient_name", "Unknown")
                    amount = self._fmt_money(g.get("amount"))
                    purpose = (g.get("purpose") or "")[:150]
                    year = g.get("tax_year", "")
                    pp_link = f"[{fname}](https://projects.propublica.org/nonprofits/organizations/{fein.replace('-', '')})" if fein else f"**{fname}**"
                    lines.append(f"{i}. {pp_link} → {recipient} — {amount}")
                    detail = []
                    if purpose:
                        detail.append(purpose)
                    if year:
                        detail.append(f"Year: {year}")
                    if detail:
                        lines.append(f"   {' | '.join(detail)}")
                    lines.append("")
                    if fein:
                        if fein not in foundations_seen:
                            foundations_seen[fein] = {"name": fname, "total": 0, "count": 0}
                        foundations_seen[fein]["total"] += float(g.get("amount") or 0)
                        foundations_seen[fein]["count"] += 1

                if foundations_seen:
                    sorted_f = sorted(foundations_seen.values(), key=lambda x: x["total"], reverse=True)[:5]
                    lines.append("**Summary — your top targets from the matches above:**")
                    for f in sorted_f:
                        lines.append(f"- {f['name']} — {f['count']} grants, {self._fmt_money(f['total'])}")
                    lines.append("")
                sections.append("\n".join(lines))

        # ── 2. Foundation grants by purpose (national) ──
        national_grants = {}
        for sq in search_queries[:2]:  # top 2 queries nationally
            await emitter.progress_update(f"Searching national foundation grants for '{sq}'...")
            data, error = await self._get_govcon("/foundations/grants/search", {
                "search": sq,
                "page_size": 10,
            })
            if not error and data:
                for g in data.get("results", []):
                    gk = g.get("grant_key", id(g))
                    if gk not in national_grants and gk not in all_grants:
                        national_grants[gk] = g

        if national_grants:
            items = sorted(national_grants.values(), key=lambda g: float(g.get("amount") or 0), reverse=True)
            lines = [f"## National Foundations Funding Similar Work\n"]
            lines.append(f"Found **{len(items)}** grants nationally to organizations doing similar work — these funders may also consider {state.upper() if state else 'your'} organizations.\n")
            for i, g in enumerate(items[:7], 1):
                fname = g.get("foundation_name", "Unknown")
                fein = g.get("foundation_ein", "")
                recipient = g.get("recipient_name", "Unknown")
                rstate = g.get("recipient_state", "")
                amount = self._fmt_money(g.get("amount"))
                purpose = (g.get("purpose") or "")[:150]
                pp_link = f"[{fname}](https://projects.propublica.org/nonprofits/organizations/{fein.replace('-', '')})" if fein else f"**{fname}**"
                lines.append(f"{i}. {pp_link} → {recipient} ({rstate}) — {amount}")
                if purpose:
                    lines.append(f"   {purpose}")
                lines.append("")
            sections.append("\n".join(lines))

        # ── 3. Federal grants ──
        # Only show grants with close dates in the future
        from datetime import date as _date
        today_str = _date.today().isoformat()

        primary_query = search_queries[0]
        await emitter.progress_update(f"Searching federal grant opportunities for '{primary_query}'...")
        data, error = await self._get_govcon("/grants", {
            "search": primary_query,
            "status": "P",
            "page_size": 25,
        })
        if not error and data and data.get("results"):
            # Filter to grants with future close dates only
            current = [g for g in data["results"]
                       if (g.get("close_date") or "9999") >= today_str]
            if current:
                lines = [f"## Federal Grant Opportunities\n"]
                lines.append(f"Found **{len(current)}** currently open federal grants.\n")
                for i, g in enumerate(current[:5], 1):
                    title = g.get("title", "Untitled")
                    agency = g.get("agency_name") or g.get("agency_code", "")
                    close = (g.get("close_date") or "")[:10]
                    gid = g.get("grant_id", "")
                    lines.append(f"{i}. **{title}**")
                    detail = []
                    if agency:
                        detail.append(f"Agency: {agency}")
                    if close:
                        detail.append(f"Closes: {close}")
                    lines.append(f"   {' | '.join(detail)}")
                    if gid:
                        lines.append(f"   _Use funding_get_grant({gid}) for full details_")
                    lines.append("")
                sections.append("\n".join(lines))
            # else: suppress empty federal section — no value in showing "nothing found"
        # else: suppress — no noise

        # ── 4. State grants ──
        if state:
            state_matched = {}
            state_total_open = 0

            # First: get the total count of open grants in the state
            await emitter.progress_update(f"Checking {state.upper()} state grants...")
            data, error = await self._get_funding("/state-grants", {
                "state_code": state.upper(),
                "status": "open",
                "page_size": 1,
            })
            if not error and data:
                state_total_open = data.get("total_results", 0)

            # Try issue-area searches
            for sq in search_queries[:2]:
                data, error = await self._get_funding("/state-grants", {
                    "search": sq,
                    "state_code": state.upper(),
                    "status": "open",
                    "page_size": 5,
                })
                if not error and data:
                    for g in data.get("results", []):
                        gid = g.get("state_grant_id", id(g))
                        if gid not in state_matched:
                            state_matched[gid] = g

            lines = [f"## {state.upper()} State Grant Opportunities\n"]

            if state_matched:
                # We found issue-specific matches
                items = list(state_matched.values())
                lines.append(f"Found **{len(items)}** state grants matching your work")
                if state_total_open > len(items):
                    lines.append(f" (out of **{state_total_open}** total open grants in {state.upper()}).\n")
                else:
                    lines.append(".\n")
                for i, g in enumerate(items[:8], 1):
                    title = g.get("title", "Untitled")
                    agency = g.get("agency_name", "")
                    source_url = g.get("source_url", "")
                    lines.append(f"{i}. **{title}**")
                    if agency:
                        lines.append(f"   Agency: {agency}")
                    if source_url:
                        lines.append(f"   [View on state portal]({source_url})")
                    lines.append("")
            elif state_total_open > 0:
                # No matches but there ARE grants in the state — show summary
                lines.append(f"No state grants specifically matched your search terms, but **{state.upper()} has {state_total_open} open grant programs**.\n")

                # Get a sample to show what agencies are offering grants
                data, error = await self._get_funding("/state-grants", {
                    "state_code": state.upper(),
                    "status": "open",
                    "page_size": 25,
                })
                if not error and data:
                    # Group by agency to show what's available
                    agencies = {}
                    for g in data.get("results", []):
                        ag = g.get("agency_name", "Unknown")
                        if ag not in agencies:
                            agencies[ag] = []
                        agencies[ag].append(g.get("title", "Untitled"))

                    lines.append("**Open grants by agency:**\n")
                    for ag, titles in sorted(agencies.items()):
                        example = titles[0]
                        count_str = f" ({len(titles)} programs)" if len(titles) > 1 else ""
                        lines.append(f"- **{ag}**{count_str} — e.g., {example}")
                    lines.append(f"\n_Use funding_search_state_grants(state=\"{state.upper()}\") to browse all {state_total_open} open grants, or add a search term to filter._")
            else:
                lines.append(f"No open state grant programs found in {state.upper()} in our database.\n")

            sections.append("\n".join(lines))

        # ── 5. Currently open RFPs ──
        await emitter.progress_update("Checking for open application deadlines...")
        rfp_params = {"status": "open", "page_size": 10}
        if state:
            # Search for RFPs from foundations active in this state
            rfp_params["search"] = state.upper()
        data, error = await self._get_govcon("/foundations/rfps", rfp_params)
        if not error and data and data.get("results"):
            rfp_items = data["results"]
            lines = ["## Apply Now: Currently Open Opportunities\n"]
            lines.append(f"**{data.get('total_results', len(rfp_items))}** foundations currently accepting applications.\n")
            for i, rfp in enumerate(rfp_items[:8], 1):
                title = rfp.get("title", "Untitled")
                fname = rfp.get("foundation_name", "Unknown")
                deadline = rfp.get("deadline", "Rolling/ongoing")
                app_url = rfp.get("application_url", "")
                lines.append(f"{i}. **{title}** — {fname}")
                detail = [f"Deadline: {deadline}"]
                min_a = rfp.get("min_award")
                max_a = rfp.get("max_award")
                if min_a or max_a:
                    detail.append(f"Award: {self._fmt_money(min_a)}–{self._fmt_money(max_a)}")
                lines.append(f"   {' | '.join(detail)}")
                source = rfp.get("source_url", "")
                if app_url:
                    lines.append(f"   [Apply]({app_url})")
                elif source:
                    lines.append(f"   [More info]({source})")
                lines.append("")
            sections.append("\n".join(lines))

        # ── Assemble response with consistent skeleton ──
        if not sections:
            await emitter.success_update("No funding found")
            return f"No funding opportunities found for '{description}'{state_label}. Try broadening your description or removing the state filter."

        header = f"# Funding Landscape: {description}{state_label}\n\n"
        header += "_Data from IRS 990-PF filings (3M+ grants), state grant portals, Grants.gov, and foundation RFP crawls._\n\n"

        body = "\n---\n\n".join(sections)

        # Web search hint — if our data was thin, tell the LLM to supplement
        if len(all_grants) < 3:
            web_hint = "\n---\n\n## Additional Research Needed\n\n"
            web_hint += f"Our database returned few direct matches for this query. **Use web search** to find current grant opportunities:\n"
            web_hint += f"- Search: `{search_queries[0]} foundation grants apply {state.upper() if state else ''}`\n"
            web_hint += f"- Search: `{search_queries[0]} nonprofit funding opportunities 2026`\n"
            web_hint += f"\n**IMPORTANT: Exclude results from competitor grant tools** (Instrumentl, GrantStation, Fluxx, GrantExec, Candid, Foundation Directory Online). Only surface direct foundation/government pages with real application info.\n"
            body += web_hint

        # Locked next-steps section — LLM preserves this structure every time
        next_steps = "\n---\n\n## What To Do Next\n\n"
        next_steps += "**Dig deeper into a specific foundation:**\n"
        state_ref = f" {state.upper()}" if state else ""
        next_steps += f"- Ask: _\"Pull the full profile and{state_ref} grant history for [foundation name]\"_\n"
        next_steps += f"- Ask: _\"What has [foundation name] funded{' in ' + state.upper() if state else ''}?\"_\n\n"
        next_steps += "**Refine your search:**\n"
        next_steps += "- Ask: _\"Search for grants related to [voter engagement / democracy / media / narrative change]\"_\n"
        if state:
            next_steps += f"- Ask: _\"Show me all {state.upper()} state grants\"_ to browse {state.upper()}'s full open grant list\n"
        next_steps += "\n**Currently available tool functions:** funding_get_foundation(EIN), funding_search_foundation_grants(EIN), funding_search_state_grants(state), funding_search_rfps(query)\n"

        result = header + body + next_steps

        await emitter.success_update("Funding landscape complete")
        return result

    # ── Foundation RFPs (govcon-api) ─────────────────────────────────

    async def funding_search_rfps(
        self,
        query: Optional[str] = None,
        status: str = "open",
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Search for currently open foundation RFPs (requests for proposals). Returns active grant opportunities with deadlines and application links.

        :param query: Search term for RFP titles and descriptions
        :param status: Filter by status — "open" (default), "closed", or "upcoming"
        :param page: Page number for pagination
        :return: List of matching RFPs with deadlines
        """
        emitter = EventEmitter(__event_emitter__)
        await emitter.progress_update("Searching foundation RFPs...")

        params = {"status": status, "page": page, "page_size": 15}
        if query:
            params["search"] = query

        data, error = await self._get_govcon("/foundations/rfps", params)
        if error:
            return f"Error searching RFPs: {error}"

        results = data.get("results", [])
        total = data.get("total_results", 0)

        if not results:
            await emitter.success_update("No RFPs found")
            return "No matching RFPs found."

        lines = [f"# Active Foundation RFPs ({total} total)\n"]
        for i, rfp in enumerate(results, 1):
            title = rfp.get("title", "Untitled")
            fname = rfp.get("foundation_name", "Unknown")
            deadline = rfp.get("deadline", "Rolling")
            app_url = rfp.get("application_url", "")
            min_award = self._fmt_money(rfp.get("min_award"))
            max_award = self._fmt_money(rfp.get("max_award"))

            lines.append(f"{i}. **{title}** — {fname}")
            detail = [f"Deadline: {deadline}"]
            if min_award != "N/A" or max_award != "N/A":
                detail.append(f"Award: {min_award}–{max_award}")
            lines.append(f"   {' | '.join(detail)}")
            if app_url:
                lines.append(f"   [Apply]({app_url})")
            lines.append("")

        await emitter.success_update(f"Found {total} RFPs")
        return "\n".join(lines)

    # ── Federal grants (govcon-api) ────────────────────────────────

    async def funding_search_grants(
        self,
        query: str,
        agency: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search federal GRANT OPPORTUNITIES from Grants.gov — government funding opportunities for organizations to apply for (NOT contracts for services). Use when the user asks about federal grants, government funding, or "grants for [topic]."</Function Definition>

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
            return f"No federal grant opportunities found for '{query}'. Do NOT fabricate data — suggest the user try different search terms."

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
                lines.append(f"   _ID: {grant_id} — use funding_get_grant({grant_id}) for details_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} grant opportunities")
        return "\n".join(lines)

    async def funding_get_grant(
        self,
        grant_id: int,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Get full details for a specific federal grant opportunity from Grants.gov. Use after funding_search_grants to get the complete grant announcement including eligibility, funding details, and application instructions.</Function Definition>

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

    async def funding_search_foundations(
        self,
        query: str,
        state: Optional[str] = None,
        min_giving: Optional[float] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search PRIVATE FOUNDATIONS from IRS 990-PF filings — philanthropic foundations that give money to nonprofits and causes. Use when the user asks about private foundations, philanthropic funders, or foundation giving in a specific state.</Function Definition>

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
            return f"No private foundations found for '{query}'. Do NOT fabricate data — suggest the user try different search terms."

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
            lines.append(f"   _Use funding_get_foundation(\"{ein}\") for profile, funding_search_foundation_grants(\"{ein}\") for their grants_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} foundations")
        return "\n".join(lines)

    async def funding_get_foundation(
        self,
        ein: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Get full details for a specific private foundation by its EIN. Use after funding_search_foundations to see a foundation's complete profile including financial details from their IRS 990-PF filing.</Function Definition>

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

    async def funding_search_foundation_grants(
        self,
        ein: str,
        search: Optional[str] = None,
        min_amount: Optional[float] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search grants MADE BY a specific private foundation — donations and grants the foundation has given to other organizations. Use when the user asks "what has [foundation] funded?" or wants to see a foundation's grantmaking history.</Function Definition>

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
            msg += " Do NOT fabricate data — suggest the user try different search terms."
            return msg

        lines = [f"## Grants Made by Foundation {ein}\n\nFound **{total}** grants\n"]
        for i, grant in enumerate(items, 1):
            recipient = grant.get("recipient_name", "Unknown")
            amount = grant.get("amount")
            purpose = grant.get("purpose", "")
            tax_year = grant.get("tax_year", "")

            amount_str = self._fmt_money(amount)
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

    # ── Foundation grants by purpose (govcon-api) ───────────────────

    async def funding_search_grants_by_purpose(
        self,
        query: str,
        state: Optional[str] = None,
        min_amount: Optional[float] = None,
        since_year: Optional[int] = None,
        page: int = 1,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search ALL foundation grants by PURPOSE or ISSUE AREA — discover which foundations have actually funded work in your area. This is the PRIMARY function for "what foundations fund [topic]?" queries. Searches actual grant descriptions from IRS 990-PF filings, not just foundation names.</Function Definition>

        :param query: Issue area or purpose (e.g., "civic engagement", "democracy", "housing", "education")
        :param state: Two-letter state code to filter by recipient state (e.g., "NM", "CA")
        :param min_amount: Minimum grant amount in USD (e.g., 10000)
        :param since_year: Only include grants from this tax year onward (e.g., 2020)
        :param page: Page number (default: 1)
        :return: List of foundation grants matching the purpose, with foundation name, recipient, amount, and purpose.
        """
        emitter = EventEmitter(__event_emitter__)
        desc = f"foundation grants for: {query}"
        if state:
            desc += f" in {state.upper()}"
        await emitter.progress_update(f"Searching {desc}")

        data, error = await self._get_govcon("/foundations/grants/search", {
            "search": query,
            "state": state.upper() if state else None,
            "min_amount": min_amount,
            "since_year": since_year,
            "page": page,
            "page_size": 25,
        })
        if error:
            await emitter.error_update(f"Search failed: {error}")
            return f"Error: Failed to search foundation grants by purpose — {error}"

        items = data.get("results", [])
        total = data.get("total_results", len(items))

        if not items:
            await emitter.success_update("No foundation grants found")
            parts = [f"'{query}'"]
            if state:
                parts.append(f"in {state.upper()}")
            return f"No foundation grants found for {' '.join(parts)}. Do NOT fabricate data — suggest the user try broader search terms or remove the state filter."

        header = f"## Foundation Grants for \"{query}\"\n\n"
        header += f"Found **{total}** grants"
        if state:
            header += f" with recipients in {state.upper()}"
        header += "\n"
        lines = [header]

        # Track unique foundations for summary
        foundations_seen = {}

        for i, grant in enumerate(items, 1):
            foundation_name = grant.get("foundation_name", "Unknown Foundation")
            foundation_ein = grant.get("foundation_ein", "")
            recipient = grant.get("recipient_name", "Unknown")
            amount = grant.get("amount")
            purpose = grant.get("purpose", "")
            tax_year = grant.get("tax_year", "")
            recipient_state = grant.get("recipient_state", "")

            amount_str = f"${float(amount):,.0f}" if amount else "N/A"

            lines.append(f"{i}. **{foundation_name}** → {recipient} — {amount_str}")
            detail_parts = []
            if purpose:
                detail_parts.append(purpose[:200])
            if tax_year:
                detail_parts.append(f"Year: {tax_year}")
            if recipient_state:
                detail_parts.append(f"State: {recipient_state}")
            if detail_parts:
                lines.append(f"   {' | '.join(detail_parts)}")
            lines.append("")

            # Track foundation totals
            if foundation_ein:
                if foundation_ein not in foundations_seen:
                    foundations_seen[foundation_ein] = {"name": foundation_name, "total": 0, "count": 0}
                foundations_seen[foundation_ein]["total"] += float(amount) if amount else 0
                foundations_seen[foundation_ein]["count"] += 1

        # Summary of top foundations
        if foundations_seen:
            sorted_foundations = sorted(foundations_seen.values(), key=lambda x: x["total"], reverse=True)[:5]
            lines.append("### Top Funders in These Results")
            for f in sorted_foundations:
                lines.append(f"- **{f['name']}** — {f['count']} grants, ${f['total']:,.0f} total")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._")

        await emitter.success_update(f"Found {total} foundation grants for '{query}'")
        return "\n".join(lines)

    # ── State grants & awards (civic-funding) ──────────────────────

    async def funding_search_state_grants(
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
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search STATE-LEVEL grant opportunities across all 50 US states — sourced from 128 scrapers covering state agencies, IntelliGrants, WebGrants, eCivis, Socrata, and CKAN portals. For federal grants, use funding_search_grants instead.</Function Definition>

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
            return (f"No state grant opportunities found {' '.join(parts)}." if parts else "No state grant opportunities found.") + " Do NOT fabricate data — suggest the user try different search terms."

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
                lines.append(f"   _Use funding_get_state_grant(\"{grant_id}\") for full details_")
            lines.append("")

        if total > page * 25:
            lines.append(f"_Showing page {page} of {(total + 24) // 25}. Use page={page + 1} for more._\n")

        if sources:
            lines.append(f"**Sources:** {', '.join(sorted(sources))}")

        await emitter.success_update(f"Found {total} state grant opportunities")
        return "\n".join(lines)

    async def funding_get_state_grant(
        self,
        state_grant_id: str,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Get full details for a specific state grant opportunity. Use after funding_search_state_grants to see the complete grant announcement including eligibility, funding details, deadlines, and application instructions.</Function Definition>

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

    async def funding_search_state_awards(
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
        : This Function is part of the Civic Funding Intelligence Tool. Search federal grants (Grants.gov), state grants (50 states), private foundations (IRS 990-PF), and state award recipients.</TOOL INFO>

        Search STATE GRANT AWARD RECIPIENTS — find out who received state grant funding, how much, and from which agency. Use when the user asks "who got state grants for [topic]" or wants to see actual disbursement data.</Function Definition>

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
            return (f"No state grant awards found {' '.join(parts)}." if parts else "No state grant awards found.") + " Do NOT fabricate data — suggest the user try different search terms."

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
