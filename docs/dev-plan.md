# Dev plan: LiveX.AI Assistant

A Python chat assistant that lets a busy founder book, list, reschedule, update and cancel cal.com meetings in plain
language. It is live on AWS, and nothing changes on the calendar until the user clicks **Confirm**.

| | |
|---|---|
| **User** | A founder who lives in their inbox and would rather type "book a 30-min intro Thursday afternoon" than click through forms |
| **Goal** | Every request resolves in a few turns, with correct times and no made-up bookings |
| **Hard rule** | Nothing changes on the calendar until the user clicks Confirm |
| **Done when** | A reviewer clones the repo, adds two keys, runs `make run`, and the demo works against a real cal.com account |
| **Status** | All five features shipped, live on AWS, 42 offline tests passing |

## Scope

| Feature | Example prompt | MCP tools |
|---|---|---|
| See meetings | "what's on my calendar tomorrow?" | `list_bookings` |
| Book | "book a 30-min intro with a candidate Thursday afternoon" | `find_available_slots`, then `create_booking` |
| Reschedule | "move it to later that day" | `find_available_slots`, then `reschedule_booking` |
| Update | "wrong email, it's jordan@lee.dev" | `update_booking` |
| Cancel | "cancel that intro" | `cancel_booking` |

A booking can also set its length (15–120 min), location, guests, notes and a meeting name. The UI adds a sidebar
with the next two weeks and a day, week and month calendar.

**Out of scope for now:** multiple users, streaming replies and Google Calendar events.

## Architecture

CloudFront serves the UI from S3 and sends `/api/*` to a FastAPI app on Lambda. Claude chooses the tools, the agent
holds every write until Confirm, and the MCP server is the only part that calls cal.com.

| Component | Choice | Why |
|---|---|---|
| Tools | Standalone MCP server wrapping cal.com v2 (`calcom_mcp/`) | Any MCP client gets the same tools and checks |
| Agent | Claude tool-use loop, capped at 8 rounds (`app/agent.py`) | Claude chooses tools; code decides what may run |
| LLM | Claude Sonnet via the Anthropic API | Bedrock was blocked on the new account; `LLM_PROVIDER=bedrock` switches back |
| Server | FastAPI on Lambda via Lambda Web Adapter | Same app on a laptop and on AWS |
| UI | One `index.html`, vanilla JS | No build step; served from S3 |
| State | In memory locally, DynamoDB on AWS | Confirm works on any Lambda instance |
| Secrets | `.env` locally, SSM SecureString on AWS | No secrets in the repo, the browser or Lambda env vars |
| Access | Access code on `/api/*` | The live demo changes a real calendar |

```
app/          FastAPI routes, agent loop + confirmation gate, LLM client, sessions, single-page UI
calcom_mcp/   cal.com v2 client, MCP server (8 tools + guards), timezone math
infra/        SAM template (CloudFront, S3, Lambda, DynamoDB) and deploy script
tests/        42 offline tests with a fake cal.com and a scripted Claude
```

## Key design decisions

The model reasons; code controls every write, every time conversion and every time shown on screen.

1. **Confirmation gate in code.** Any tool not marked `readOnlyHint` in its MCP annotations is held as a pending
   action and shown as a card. It runs only after Confirm (or a typed "yes"). Cancel, or changing the subject, tells
   Claude it was not executed.
2. **Safety checks in the MCP server.** Before a write it re-checks the slot against live availability, checks the
   booking uid is still active, validates emails and guests, and rejects past times. Errors say how to recover, so
   any MCP client gets the same protection.
3. **No date math in the LLM.** Every turn carries the timezone, the current time and a 14-day date table. Claude
   passes local dates and `part_of_day` (morning 8–12, afternoon 12–17, evening 17–21); Python converts to UTC for
   cal.com and back to labels like "Thu Oct 22, 2:00 PM PDT".
4. **Only real times on screen.** Final replies are structured JSON, and the UI keeps only times cal.com actually
   returned.
5. **Same app locally and on AWS.** Lambda Web Adapter runs FastAPI unchanged; DynamoDB sessions let a Confirm land
   on any Lambda instance.

**Working around cal.com's API limits:**

| Limit | Workaround |
|---|---|
| Each endpoint needs its own `cal-api-version` | Pinned per client method: event types `2024-06-14`, slots `2024-09-04`, list `2026-05-01`, writes `2026-02-25` |
| A reschedule creates a new booking uid | The agent tracks the new uid; a stale uid gets an error naming its replacement |
| No way to set a booking title | Each named meeting is booked on a hidden event type carrying the name |
| No way to change an attendee | `update_booking` books a corrected copy, then cancels the original |

## Delivery and testing

Built in four phases, each tested before the next. All four are done.

1. **Core:** cal.com client, MCP server and tools, agent with the confirmation gate, chat UI, offline tests.
2. **Cloud:** SAM template with CloudFront, S3, Lambda and DynamoDB; secrets in SSM; an access code on `/api/*`.
3. **Product:** LiveX theme, time picker, day/week/month calendar, booking length, location, guests and names.
4. **Harden:** structured replies, `update_booking`, fixes from production logs, live end-to-end runs.

Every change follows the same loop: `make test` → `make deploy` → use it on the live site → check CloudWatch logs →
fix or commit.

| Test layer | What it covers |
|---|---|
| Offline pytest (42 tests, under 2 s) | Fake cal.com + scripted Claude: the gate, slot re-checks, timezone math, uid tracking, pagination |
| Live API probes | Real cal.com behaviour the docs don't spell out |
| End-to-end runs | Book, list, update, move, cancel on the deployed API, then clean up |
| Visual checks | Headless Chrome in light, dark and phone width |
| Production logs | CloudWatch on the live Lambda |

| Risk | Mitigation |
|---|---|
| The model invents a time or booking | Only cal.com-returned times reach the UI; uids are checked against live data |
| A write happens without consent | Gate enforced in code; MCP server re-checks every write |
| The public URL can change a real calendar | Access code on the API; secrets only in SSM |
| Bedrock blocked on the new AWS account | Anthropic API today; `LLM_PROVIDER=bedrock` switches back |
| Lambda cold start (~5 s) | Hash-based .pyc and async init in the Lambda Web Adapter; warm the site up before a demo |

## Next steps

1. **Evals in CI:** scripted conversations scored on no unconfirmed writes and no invented times.
2. **Streaming replies,** so text appears as it is written.
3. **Multiple users,** with Cognito sign-in and per-user cal.com OAuth.
4. **Bedrock,** once AWS lifts the account block.
