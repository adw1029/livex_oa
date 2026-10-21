# Dev plan — Cal.com chat assistant (2-hour build)

A Python chatbot that lets a busy founder manage cal.com in plain language: **book, list, cancel, reschedule**, in a web UI.

Scope rule: get the core experience working end to end first. MCP and AWS only go in where they cost almost nothing. Everything else goes in the README's "Production path" section.

## Stack

| Piece | Choice |
|---|---|
| Server | FastAPI, Python 3.12, `httpx`, Pydantic, `uv` |
| Tools | **MCP server** (official `mcp` 2.x SDK, `MCPServer`) wrapping the cal.com v2 API |
| Agent | MCP client (stdio) + Claude via `anthropic` SDK tool use |
| LLM provider | `LLM_PROVIDER=anthropic` (default) or `bedrock` (`AnthropicBedrock()`, IAM auth, same code) |
| UI | Single `static/index.html` (vanilla JS) served by FastAPI, one port |
| State | In-memory sessions + pending actions keyed by `session_id` |
| Secrets | `.env` (never sent to the browser) |

## Repo layout

```
livex_oa/
  calcom_mcp/
    client.py      # async cal.com v2 client, per-endpoint API versions
    server.py      # FastMCP server: tools + guards + annotations
  app/
    main.py        # FastAPI: /api/chat, /api/confirm, static UI
    agent.py       # LLM loop, MCP client, confirmation gate
    timectx.py     # "now" + 7-day date table in user's timezone
    static/index.html
  tests/           # (if time) fake cal.com, confirmation-gate tests
  .env.example
  README.md
```

## cal.com API (verified against docs)

Each endpoint has its own `cal-api-version`. Pin the version per method, not in one env var.

| Method | HTTP | `cal-api-version` |
|---|---|---|
| list event types | `GET /v2/event-types` | `2024-06-14` |
| slots | `GET /v2/slots` | `2024-09-04` |
| list bookings | `GET /v2/bookings` | `2026-05-01` |
| create | `POST /v2/bookings` | `2026-02-25` |
| reschedule | `POST /v2/bookings/{uid}/reschedule` | `2026-02-25` |
| cancel | `POST /v2/bookings/{uid}/cancel` | `2026-02-25` |

Gotchas:
- Times go to cal.com in **UTC ISO 8601**. Do the timezone conversion in Python, never in the LLM.
- Reschedule returns a **new booking uid**. Track it so "cancel that intro" still works afterward.
- Pass `rescheduledBy=<host email>` so the rescheduled booking is auto-confirmed.
- Use `bookingUidToReschedule` on slots so the original time still shows as free.
- Create requires `attendee.name` + `attendee.timeZone`. Require the email too, so the invite actually reaches the attendee.

## MCP tools

| Tool | Annotation | Notes |
|---|---|---|
| `list_event_types()` | read-only | picks the event type id |
| `find_available_slots(event_type_id, date_from, timezone, date_to?, part_of_day?, after_time?, reschedule_booking_uid?)` | read-only | slots grouped by day, labeled in local time |
| `list_bookings(timezone, date_from?, date_to?, status?)` | read-only | compact: uid, title, local start/end, attendee |
| `create_booking(start, attendee_name, attendee_email, timezone)` | destructive | re-checks slot is free before posting |
| `reschedule_booking(booking_uid, new_start, timezone)` | destructive | validates uid exists; returns new uid |
| `cancel_booking(booking_uid, timezone, reason?)` | destructive | validates uid exists |

`part_of_day` is mapped in code: morning 8–12, afternoon 12–17, evening 17–20.

## Agent + confirmation gate

1. Build the system prompt with the current time and a 7-day date table in the user's timezone, plus rules: ask only for missing details, offer 2–4 slots, never invent times or uids.
2. Tool-use loop over the MCP tools (cap ~8 rounds).
3. **If the model calls a `destructiveHint` tool, don't run it.** Store it as a pending action, return a summary ("Book Intro with Jordan Lee, Thu 2:00pm PT?"), and the UI shows **Confirm / Cancel** buttons.
4. `POST /api/confirm/{action_id}` runs the stored call via MCP and feeds the result back to the model for a final reply.

The gate is enforced in code from the tool annotations, not by the prompt. A write can't happen without a click.

## API contract

`POST /api/chat` `{session_id, message, timezone}` returns:
```json
{ "reply": "...", "suggestions": ["2:00pm", "3:30pm"], "pending_action": {"id": "...", "summary": "..."} | null }
```
`POST /api/confirm/{id}` `{session_id, approve: bool}` returns the same shape.

## UI (single page)

- Chat bubbles with light markdown, a typing indicator, and an error toast.
- Suggestion chips (slot times) that send on click.
- A confirmation card with Confirm/Cancel when `pending_action` is set.
- Empty state: the four example prompts from the assignment.
- Timezone from `Intl.DateTimeFormat().resolvedOptions().timeZone`, `session_id` in `sessionStorage`.

## Timeline (~2h)

| Time | Task |
|---|---|
| 0:00–0:15 | cal.com account, API key, "30-min Intro" event type, 1–2 seed bookings, curl smoke test |
| 0:15–0:45 | `calcom_mcp/` client + server; poke the tools with MCP Inspector |
| 0:45–1:15 | Agent loop, time context, confirmation gate, `/api/chat` + `/api/confirm` |
| 1:15–1:40 | `index.html` chat UI |
| 1:40–2:00 | Run the demo script end to end, write the README |

If time is left: pytest with a fake cal.com client covering "no write without confirm", "reschedule tracks new uid", and "slot taken → re-offer".

## Demo script

1. `what's on my calendar tomorrow?`
2. `book a 30-min intro with a candidate Thursday afternoon` → give `Jordan Lee, jordan@example.com` → pick a slot → **Confirm**
3. `move it to later that day` → pick → **Confirm**
4. `cancel that intro` → **Confirm**

Check each step in the cal.com dashboard: the booking appears, moves, then is cancelled.

## README must include

- Setup: `uv sync`, `.env`, `uv run uvicorn app.main:app`, open `localhost:8000`.
- **Design decisions:** the code-enforced confirmation gate, MCP as the tool boundary, timezone handling in Python, per-endpoint API versions.
- **Use it from Claude Desktop:** an `mcpServers` config snippet pointing at `calcom_mcp/server.py`.
- **Production path on AWS (not built):** Bedrock (already supported via flag), Lambda + Web Adapter with response streaming behind CloudFront, DynamoDB for sessions/pending actions/audit log, Secrets Manager, an auth gate on the URL, CDK in Python, GitHub Actions with OIDC deploys.
- **Known limitations:** only shows cal.com bookings (not the connected Google Calendar), single user, in-memory state.
- **With more time:** streaming responses, an eval suite of scripted conversations against a fake cal.com, React UI.

## Definition of done

A reviewer clones the repo, fills in `.env`, runs one command, opens the browser, and the four demo chats work against a real cal.com account. No action changes the calendar without an explicit Confirm click.
