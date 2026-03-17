# Civic Funding Intelligence Tool

Open WebUI tool for searching federal grants (Grants.gov) and private foundations (IRS 990-PF).

Part of the [Civic Intelligence Platform](https://github.com/unreliable-machine/civic-tools) for [Change Agent AI](https://thechange.ai).

## Installation

1. Open your Open WebUI instance → **Admin Panel** → **Tools** → **+**
2. Paste the contents of `civic_funding.py`
3. Save → configure Valves (gear icon)

## Valves

| Valve | Value |
|-------|-------|
| `GOVCON_API_URL` | `https://govcon-api-production.up.railway.app` |
| `GOVCON_API_KEY` | Your GOVCON API key |
| `TIMEOUT` | `30` |

## Methods

- `search_grants`
- `get_grant`
- `search_foundations`
- `get_foundation`
- `search_foundation_grants`

## Test

```
Search for federal grants related to housing
```

## Backend API

`govcon-api`

## Related

- [civic-tools](https://github.com/unreliable-machine/civic-tools) — umbrella repo with all 7 civic tools
- [civic-finance](https://github.com/unreliable-machine/civic-finance) — campaign finance microservice
- [civic-irs](https://github.com/unreliable-machine/civic-irs) — IRS 990 filings microservice
- [govcon-intelligence](https://github.com/unreliable-machine/govcon-intelligence) — procurement, grants, legislators, courts

## License

MIT
