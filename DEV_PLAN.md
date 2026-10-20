# Initial fullstack plan — Cal.com chat assistant

Python backend + React chat UI. Goal: the four assignment prompts work in the browser like a sharp EA. No AWS, Okta, MCP, or Jev in v1.

## Stack

| Piece | Choice |
|---|---|
| API | FastAPI, Python 3.12, `httpx`, Pydantic |
| LLM | OpenAI or Anthropic **tool calling** (one provider) |
| UI | Vite + React + TypeScript |
| Secrets | `.env` — never in the client |
| State | In-memory chat sessions (dict keyed by `session_id`) |

## Repo

```
livex_oa/
  backend/
    app/
      main.py          # FastAPI, CORS, static later
      config.py        # env
      calcom.py        # Cal.com v2 client
      tools.py         # tool schemas + handlers
      agent.py         # LLM loop + mutation guards
      models.py        # chat request/response
    requirements.txt
  frontend/
    src/
      App.tsx
      api.ts
      components/Chat.tsx
      components/Message.tsx
      components/BookingCard.tsx
  .env.example
  README.md
```

Run: `uvicorn` on `:8000`, Vite on `:5173` proxying `/api`.

## Cal.com (do this before UI)

### Account setup

1. Cal.com account + API key (`Settings → Security`).
2. One **30-min Intro** event type, timezone set (e.g. `America/Los_Angeles`).
3. Seed 1–2 real bookings so list/cancel/reschedule aren’t empty.

### Client methods (`calcom.py`)

| Method | HTTP |
|---|---|
| `list_event_types` | `GET /v2/event-types` |
| `get_slots` | `GET /v2/slots` |
| `list_bookings` | `GET /v2/bookings` |
| `create_booking` | `POST /v2/bookings` |
| `cancel_booking` | `POST /v2/bookings/{uid}/cancel` |
| `reschedule_booking` | `POST /v2/bookings/{uid}/reschedule` |

Headers: `Authorization: Bearer <key>`. Use the `cal-api-version` the docs require (bookings `2026-02-25`, slots `2024-09-04` unless you confirm otherwise).

Prove with a `scripts/smoke.py` that prints event types, tomorrow’s slots, and existing bookings.

## Backend contract

`POST /api/chat`

```json
{
  "session_id": "uuid",
  "message": "book a 30-min intro Thursday afternoon",
  "timezone": "America/Los_Angeles"
}
```

Response (v1 can be non-streaming JSON; add SSE in P1):

```json
{
  "reply": "I can do Thu 2:00, 3:30, or 4:00pm PT. Which works?",
  "suggestions": ["Book 2:00pm", "Book 3:30pm", "Show Friday instead"]
}
```

Optional later: `GET /api/health`.

### Agent loop

1. Append user message to session history.
2. Call LLM with system prompt + tools.
3. Execute tools → append results → repeat until a final text reply (cap ~8 rounds).
4. Return assistant text + suggestion chips.

### Tools the model gets

- `list_event_types`
- `get_slots(event_type_id, start, end, timezone, duration?, booking_uid_to_reschedule?)`
- `list_bookings(after_start?, before_end?, status?)`
- `create_booking(...)`
- `cancel_booking(uid, reason?)`
- `reschedule_booking(uid, start, timezone?)`

### Guards in code (not another model)

- Create/reschedule only if `start` was in the last `get_slots` result for that session (or re-fetch and check).
- Cancel/reschedule only with a `bookingUid` that appeared in `list_bookings` this session (or re-fetch).
- If attendee name/email missing → no create; model must ask.
- Writes: model must confirm in chat (“Book X at Y?”) unless the user already said yes.

### System prompt (short)

You are the founder’s scheduler. Times in the user’s timezone. Ask only for missing details. Offer 2–4 slots, not a dump. Confirm before book/cancel/reschedule. Never invent times or uids.

## Frontend

One screen: transcript + composer.

- Send `timezone` from `Intl.DateTimeFormat().resolvedOptions().timeZone`.
- Persist `session_id` in `sessionStorage`.
- User bubbles / assistant bubbles; markdown-light (bold, lists).
- Loading state while `/api/chat` runs.
- Suggestion chips under the last assistant message; click fills/sends.
- Error toast if the API or Cal.com fails.
- Empty state: four example prompts from the assignment.

P1: booking cards (title, local time, attendee, link) if you later return structured `bookings[]` from the API. v1 text-only is enough if the copy is good.

## Build order

### Day 0 — setup (~1h)

- Cal.com key + Intro event type
- `.env.example`: `CAL_API_KEY`, `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, `CAL_API_VERSION`
- FastAPI hello + React hello + CORS/proxy

### Day 1 — Cal.com client

- Implement six methods
- `smoke.py` green
- Handle Cal.com error bodies as readable strings

### Day 2 — agent

- Tool schemas + loop
- Chat endpoint + in-memory sessions
- Curl the four intents until they work without a UI

### Day 3 — UI

- Chat, timezone, chips, errors
- Click through the four prompts in the browser

### Day 4 — product polish (P1)

- Slot wording + timezone labels
- Stricter guards
- Empty calendar / no slots / slot taken
- README: run steps + demo script

### Stretch (only if P0 is demo-ready)

- SSE streaming
- Booking cards
- Serve `frontend/dist` from FastAPI (one-port demo)

## Demo script

1. `what's on my calendar tomorrow?`
2. `book a 30-min intro with a candidate Thursday afternoon` → supply `Jordan Lee, jordan@example.com` when asked → pick a slot → confirm
3. `move my 3pm to later today` (or the time you just booked)
4. `cancel that intro`

Record expected Cal.com side effects (event appears / moves / disappears).

## Out of scope for v1

Auth, DB, AWS, Bedrock, MCP, Jev, calendar grid, email/Slack.

## Definition of done

A reviewer clones, copies `.env`, runs backend + frontend, and those four chats work against a real Cal.com account. No terminal-only path required.
