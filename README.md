# LiveX.AI Assistant

**Conversational scheduling for cal.com.** A chat assistant for a busy founder who would rather type
*"book a 30-min intro with a candidate Thursday afternoon"* than click through forms. It **books**, **lists**,
**reschedules**, **updates** and **cancels** cal.com events in plain language, from a web UI.

**Live demo:** https://d8f0ss1s0decm.cloudfront.net (the access code is in the submission email)

![LiveX.AI Assistant: Smart view sidebar, a time picker with every open time, and a booking confirmation card](docs/screenshot.png)

## Highlights

- **Plain-language scheduling.** Relative dates ("tomorrow", "Thursday afternoon", "later today") resolve in your
  timezone. For a vague request ("book a quick call with someone"), the assistant first asks for the day and length,
  with tap-able options. Once those are known, it shows every open time that day as a tap-able button.
- **Nothing changes without a click.** Every create, reschedule, update and cancel shows a confirmation card first. Code
  enforces this, not the prompt.
- **Smart view and Calendar.** A switch under the date flips between the two modes.
  - **Smart view** (next to the chat): what's up next, with a countdown and a join link, and your next two weeks of
    meetings, with one-click Move and Cancel.
  - **Calendar:** a mini month with dots on busy days, and a full day, week or month calendar with a "now" line and
    overlapping meetings side by side. Click a meeting for details, Join, Move or Cancel; click an empty time to start
    a booking in the chat.
- **MCP at the core.** The cal.com tools are a standalone [MCP](https://modelcontextprotocol.io) server. The web app is
  one client, and the same server works in Claude Desktop or Claude Code.
- **Deployed on AWS.** CloudFront, S3, Lambda, DynamoDB and SSM Parameter Store, all pay-per-request.

## Try it

| You type | What happens |
|---|---|
| `what's on my calendar tomorrow?` | Lists tomorrow's meetings in your timezone |
| `book a 30-min intro with a candidate Thursday afternoon` | Picks the event type, offers open Thursday times 12–5pm, asks who it's with |
| `Jordan Lee, jordan@example.com` → pick a time | Shows a confirmation card. Nothing is booked until you click **Book it** |
| `move it to later that day` | Finds later open times, then shows a reschedule card |
| `wrong email, it's jordan@lee.dev` | Shows an update card with the change; confirm and the meeting is rebooked with the right email |
| `cancel that intro` | Shows a cancel card; confirm and it's cancelled |

Each change is visible right away in the cal.com dashboard. The attendee gets cal.com's usual emails, so use an
address that can receive mail: cal.com rejects addresses like `@example.com`.

## How it works

```
Browser (chat UI)
   │  /api/chat, /api/confirm, /api/agenda
   ▼
FastAPI app ── Agent: Claude tool-use loop + confirmation gate
   │  MCP over stdio
   ▼
calcom_mcp server ── 8 tools, safety checks, timezone math
   │  HTTPS
   ▼
cal.com v2 API
```

| MCP tool | Purpose | Gated? |
|---|---|---|
| `get_profile` | Owner's name, email, timezone | no |
| `list_event_types` | Bookable event types: length options and allowed locations | no |
| `find_available_slots` | Open times for a local date range, part of day, or "after 3pm" | no |
| `list_bookings` | Meetings by date range, status, attendee name or email, or event type | no |
| `create_booking` | Book a meeting: attendee, guests, length, location; can double-book on request | **yes** |
| `reschedule_booking` | Move a booking (cal.com issues a new uid) | **yes** |
| `update_booking` | Fix a meeting's name or attendee name/email: rebooks it identically, then cancels the original | **yes** |
| `cancel_booking` | Cancel a booking | **yes** |

## Quick start

Requirements: Python 3.11+, a [cal.com](https://cal.com) account with at least one event type, and an
[Anthropic API key](https://console.anthropic.com).

```bash
make install            # creates .venv and installs dependencies
cp .env.example .env    # then set CAL_API_KEY and ANTHROPIC_API_KEY
make run                # http://localhost:8000
```

Create the cal.com key under *Settings → Security → API keys* (live keys start with `cal_live_`).

```bash
make test               # 41 tests, no network or API keys needed
```

## Design decisions

**1. The confirmation gate is enforced in code.**
Every MCP tool declares annotations. Any tool not marked `readOnlyHint` (create, reschedule, update, cancel) is intercepted:
the model's `tool_use` is held as a *pending action*, the UI shows a confirmation card, and the call runs only after
`POST /api/confirm`. Typing "yes" also confirms. Typing anything else closes the pending call with an honest
"not executed" result, so the model never sees a fake success. A change that is missing a required detail (say, a
booking with no start time) never becomes a card; the model is told what's missing and tries again. See `app/agent.py`.

**2. Safety checks live in the MCP server, so every client gets them.**
Before any write, the server re-checks the slot against live availability. It also checks that booking uids exist
and are still active, validates the attendee email, and rejects past times. Errors are written for the model to
recover from, e.g. "That slot is no longer available — call find_available_slots again". See `calcom_mcp/server.py`.

**3. The model never does timezone math.**
Each user turn carries a short note with the user's timezone, the current local time, and a 14-day date table.
Tools take *local* dates plus `part_of_day` (morning 8–12, afternoon 12–17, evening 17–21) or `after_time`.
Python converts to UTC and returns readable labels like `Thu Oct 22, 2:00 PM`. Slot `start` values pass through
to booking unchanged. See `calcom_mcp/timeutil.py`.

**4. Details of the cal.com API are handled carefully.**
- **API versions:** each endpoint has its own `cal-api-version` (slots `2024-09-04`, bookings list `2026-05-01`,
  writes `2026-02-25`, event types `2024-06-14`). A wrong header silently falls back to an older response shape,
  so each client method pins its own version.
- **Slot windows:** slot queries only return slots that fit entirely inside the window, so the availability
  re-check uses a window with room for the whole meeting.
- **Reschedules:** they pass `rescheduledBy=<host email>`, so the moved booking is auto-confirmed. The new uid cal.com
  issues is returned to the model, so "cancel that intro" still works after a move.
- **Cancellations:** cal.com requires a reason when the host cancels, so the server supplies a default.
- **Meeting names:** a booking's title can't be set or changed through the API, because cal.com derives it from the
  event type. So each meeting is booked on a hidden one-off event type named after it ("Investor sync", or a name
  Claude picks, like "Intro call with Andy"), with a name template of just `{Event type title}`. It copies the
  original type's length, locations and schedule. The one-off type is deleted if the booking fails, or once its
  meeting is cancelled, and it never shows up as a bookable type.
- **Corrections:** cal.com can't edit a booking's attendee or name. `update_booking` books an identical meeting
  (same time, length, location, guests, notes) with the fix, using `allowConflicts` because the original still holds
  the slot, and only then cancels the original. If the new booking fails, the original is untouched.
- **Stale uids:** a rescheduled booking keeps a `rescheduledToUid`. If the model acts on the old uid, the server's
  error names the new one, so the model can recover.
- **Durations:** `lengthInMinutes` is only accepted for event types with `lengthInMinutesOptions`. The slots API
  returns slots of any `duration`, even for fixed-length types, and booking one of them then fails. So the server
  checks the length against the event type first. For any length on request, give one event type several durations
  (e.g. a hidden "Flexible meeting").
- **Locations:** a booking's `location` must be one of the event type's own locations. The server lists those
  options and validates against them, and it explains when a new kind of place (e.g. "custom attendee location")
  must first be added to the event type in cal.com.
- **Host overrides:** "book it anyway" sets `allowConflicts`. The confirmation card warns that the booking
  overlaps another meeting.
- **Recurring event types:** booking one returns every occurrence (cal.com returns a list).
- **Bookings list:** the server follows cursor pagination (`pagination.nextCursor`), so long ranges aren't cut off
  at 100. It also exposes the `attendeeEmail`, `eventTypeId` and `status` filters, and returns notes, cancellation
  reasons and reschedule links.

**5. Structured replies, rendered by the app.**
The model's final answer uses Claude's structured outputs (a JSON schema): a one- or two-sentence `message`, the
`times` it suggests, the `meetings` it refers to, and up to five tap-able `replies`. The app renders a picker with
every open time on the day Claude settles on, and meetings as cards with a Join link, so
formatting stays consistent and the model can't ramble.
The server keeps only times that cal.com actually returned and meetings it actually fetched, so a hallucinated slot
can't reach the screen. If the model repeats an identical lookup within one request, the server returns "already
fetched" instead of looping. If it runs out of tool rounds, it is asked to answer from what it already has.

## Deployment on AWS

```
Browser ──▶ CloudFront ──┬─ /*      ─▶ S3 (private, Origin Access Control): index.html, logo
                         └─ /api/*  ─▶ Lambda Function URL ─▶ FastAPI via Lambda Web Adapter
                                         ├─ MCP server (stdio subprocess) ─▶ cal.com
                                         ├─ Claude API
                                         ├─ SSM Parameter Store (SecureString): API keys, access code
                                         └─ DynamoDB: chat sessions + pending confirmations (24h TTL)
```

- **One origin.** CloudFront serves the UI and the API from the same domain, so there's no CORS. The UI is cached at
  the edge; `/api/*` is never cached.
- **The same app runs locally and on Lambda.** Lambda Web Adapter runs the unchanged FastAPI app.
- **Secrets stay in SSM.** The Lambda role can read only `/livex-assistant/*`. Secrets are loaded at cold start and
  never stored in env vars or the repo.
- **Sessions live in DynamoDB.** Lambda may run several copies of the app, and a Confirm click can reach a different
  copy than the chat turn that created it. Each request loads and saves the session, so the gate still holds.
- **Access code.** The URL can change a real calendar, so every `/api/*` call needs the code. The UI asks for it once
  and has a Log out button.
- **Cost.** About $0 at demo traffic: everything is pay-per-request.

**Deploy your own.** You need the AWS CLI, SAM CLI and Python 3.11+. Docker is not needed.

1. Create the secrets once (CloudFormation can't create SecureStrings), then paste the real values in
   *Systems Manager → Parameter Store*:
   ```bash
   for n in CAL_API_KEY ANTHROPIC_API_KEY ACCESS_PASSCODE; do
     aws ssm put-parameter --type SecureString --name /livex-assistant/$n --value REPLACE_ME
   done
   ```
2. Run `make deploy`. This builds the Lambda bundle, deploys `infra/template.yaml`, uploads the UI, and prints the
   URL. Run it again after changing a secret.

### Claude API or Amazon Bedrock

`LLM_PROVIDER=anthropic` (the default) uses the Claude API with `claude-sonnet-5-5`. `LLM_PROVIDER=bedrock` runs the
same Messages API through Amazon Bedrock, authenticated by your AWS profile or the Lambda role, so no AI key is stored
anywhere. Both use the same code path (`app/llm.py`). The Bedrock path is wired and authenticates, but this new AWS
account's Bedrock model access was still pending approval, so the demo runs on the Claude API.

## Use the MCP server from Claude Desktop or Claude Code

The tools aren't tied to this UI. Add the server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "calcom": {
      "command": "/absolute/path/to/repo/.venv/bin/python",
      "args": ["-m", "calcom_mcp.server"],
      "cwd": "/absolute/path/to/repo",
      "env": { "CAL_API_KEY": "cal_live_..." }
    }
  }
}
```

For Claude Code: `claude mcp add calcom -e CAL_API_KEY=cal_live_... -- /abs/path/.venv/bin/python -m calcom_mcp.server`.
To inspect the tools interactively: `npx @modelcontextprotocol/inspector .venv/bin/python -m calcom_mcp.server`.

## Project layout

```
app/
  main.py          FastAPI endpoints, access-code check, MCP subprocess lifecycle
  agent.py         Claude tool-use loop + confirmation gate
  llm.py           Claude API / Bedrock switch
  config.py        settings from .env locally, SSM on AWS
  sessions.py      session storage: in memory locally, DynamoDB on Lambda
  static/          chat UI (single HTML file, vanilla JS, light/dark) + logo
calcom_mcp/
  server.py        MCP server: tools, annotations, safety checks
  client.py        cal.com v2 client: per-endpoint API versions, readable errors
  timeutil.py      timezone math and human-readable labels
tests/             fake cal.com + scripted LLM: gate, guards, sessions
infra/
  template.yaml    AWS SAM: CloudFront, S3, Lambda, DynamoDB, IAM
  deploy.sh        build + deploy (make deploy)
  run.sh           Lambda entry point
docs/              assignment brief, screenshot
```

## Limitations and next steps

- **Only cal.com bookings are listed.** Events on a connected Google Calendar don't appear in the list, but they
  still block availability, because cal.com accounts for them in slots.
- **Single user.** One cal.com account and a shared access code. For multiple users: Cognito sign-in, plus
  per-user cal.com OAuth.
- **No streaming yet.** Replies arrive whole after 2–6 seconds; a typing indicator covers the wait. Next: stream
  tokens and tool progress ("Checking Thursday…") over SSE.
- **Cold starts.** The first request after the Lambda has been idle takes about 10 seconds.
- **Evals.** Next: scripted multi-turn conversations scored on tool sequence, no unconfirmed writes, and no invented
  times, run in CI.
