# LiveX.AI Assistant — conversational scheduling for cal.com

A chat assistant for a busy founder who'd rather type *"book a 30-min intro with a candidate Thursday afternoon"* than click through forms. It can **book**, **list**, **reschedule**, and **cancel** cal.com events in plain language, from a web UI.

```
Browser (single-page chat UI)
   │  POST /api/chat, /api/confirm
   ▼
FastAPI app ── Agent: Claude tool-use loop + confirmation gate
   │  MCP (stdio)
   ▼
calcom_mcp server ── 7 tools, guards, timezone math
   │  HTTPS
   ▼
cal.com v2 API
```

## Quick start

Requires Python 3.11+, a cal.com account, and an Anthropic API key.

1. **cal.com setup.** Create an API key in *Settings → Security* (live keys start with `cal_live_`). Make sure you have at least one event type, for example "30 Min Intro", and set your timezone in your profile.
2. **Configure.**
   ```bash
   cp .env.example .env    # fill in CAL_API_KEY and ANTHROPIC_API_KEY
   ```
3. **Run.**
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/uvicorn app.main:app --reload
   # or with uv:  uv sync && uv run uvicorn app.main:app --reload
   ```
4. Open **http://localhost:8000**.

Tests need no keys or network: `.venv/bin/python -m pytest` (or `uv run pytest`).

## Demo script

| You type | What happens |
|---|---|
| `what's on my calendar tomorrow?` | Lists tomorrow's bookings in your timezone |
| `book a 30-min intro with a candidate Thursday afternoon` | Picks the Intro event type, finds Thursday 12–5pm slots, offers 2–4 as clickable chips, and asks who it's with |
| `Jordan Lee, jordan@example.com` → pick a time | Shows a **confirmation card**. Nothing is booked until you click Confirm |
| `move it to later that day` | Finds later slots (keeping the original time open), then shows a reschedule card |
| `cancel that intro` | Shows a red cancel card. Confirm, and the booking is cancelled |

Each step is visible right away in the cal.com dashboard.

## Design decisions

**1. Nothing changes the calendar without an explicit click, and code enforces it, not the prompt.**
Every MCP tool declares annotations. Any tool not marked `readOnlyHint` (create, reschedule, cancel) is intercepted by the agent. The model's `tool_use` is held as a *pending action*, the UI renders a confirmation card, and the call runs only after `POST /api/confirm`. If the user types "yes", that counts as Confirm. If they type anything else, the pending call is closed with an honest "not executed" result, so the model never sees a fake success and the transcript stays valid. See `app/agent.py`.

**2. MCP is the tool boundary.**
All cal.com logic lives in a standalone MCP server (`calcom_mcp/server.py`, official `mcp` SDK). The chat app is just one MCP client: it discovers the tools at startup and builds the confirmation gate from their annotations. The same server works unchanged in Claude Desktop or Claude Code (see below).

**3. Safety checks live in the server, so every client gets them.**
Before any write, the server re-checks the slot against live availability. It verifies that booking uids exist and are still active, validates the attendee email, rejects past times, and returns clear errors the model can recover from ("That slot is no longer available — offer new times").

**4. The model never does timezone math.**
Each user turn carries a short context note: the user's timezone, the current local time, and a 14-day date table, so "Thursday" and "tomorrow" resolve reliably. Tools take *local dates* plus `part_of_day` (morning 8–12, afternoon 12–17, evening 17–21) or `after_time` ("later today"). Python converts them to UTC and returns human labels such as `Thu Oct 23, 2:00 PM`. Slot `start` values pass through to booking unchanged.

**5. Correct cal.com API usage.**
- **API versions:** cal.com versions each endpoint separately: slots `2024-09-04`, bookings list `2026-05-01`, create/reschedule/cancel `2026-02-25`, event types `2024-06-14`. A wrong header silently falls back to an older response shape, so each method pins its own version (`calcom_mcp/client.py`).
- **Auto-confirmed reschedules:** reschedules pass `rescheduledBy=<host email>`, so the moved booking is auto-confirmed instead of waiting on the host.
- **New uid after reschedule:** cal.com issues a new booking uid when a booking is rescheduled. The tool returns it, and the model is told to use it for follow-ups ("cancel that intro" after a move still works).
- **Original slot stays visible:** when rescheduling, slot searches pass `bookingUidToReschedule`, so the meeting's current time still counts as available.

**6. UX choices for a busy founder.**
- **Offers, not dumps:** 2–4 well-spread options.
- **Clickable replies:** suggested replies render as chips.
- **Few questions:** only missing details are asked for, all in one message.
- **Readable times:** shown in your timezone with the zone abbreviation.

## Use the MCP server from Claude Desktop / Claude Code

The tools aren't tied to this UI. Add the server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "calcom": {
      "command": "/absolute/path/to/livex_oa/.venv/bin/python",
      "args": ["-m", "calcom_mcp.server"],
      "cwd": "/absolute/path/to/livex_oa",
      "env": { "CAL_API_KEY": "cal_live_..." }
    }
  }
}
```

Or add it to Claude Code: `claude mcp add calcom -e CAL_API_KEY=cal_live_... -- /abs/path/.venv/bin/python -m calcom_mcp.server`

You can also inspect it interactively with `npx @modelcontextprotocol/inspector .venv/bin/python -m calcom_mcp.server`.

## LLM providers: Claude API or Amazon Bedrock

`LLM_PROVIDER=anthropic` (default) uses the Claude API with `claude-sonnet-5-5`. `LLM_PROVIDER=bedrock` runs the same Messages API through Amazon Bedrock (`AnthropicBedrockMantle`). It authenticates with your AWS profile (`AWS_PROFILE`, e.g. an IAM Identity Center SSO profile), so no AI keys are stored. The code path is the same either way (`app/llm.py`).

> Status: the Bedrock path is wired and authenticates via SSO. My new AWS account's Bedrock model access was still pending approval at submission time, so the demo runs on the Claude API.

## Deployed on AWS

Live at **https://d8f0ss1s0decm.cloudfront.net** (access code in the submission note).

```
Browser ──▶ CloudFront ──┬─ /*      ─▶ S3 (private, Origin Access Control): index.html, logo
                         └─ /api/*  ─▶ Lambda Function URL ─▶ FastAPI via Lambda Web Adapter
                                         ├─ MCP server (stdio subprocess) ─▶ cal.com
                                         ├─ Claude API
                                         ├─ SSM Parameter Store (SecureString): API keys, access code
                                         └─ DynamoDB: chat sessions + pending confirmations (24h TTL)
```

- **One origin.** CloudFront serves the UI and the API from the same domain, so there's no CORS. The UI is cached at the edge; `/api/*` is never cached.
- **Same app, no rewrite.** Lambda Web Adapter runs the FastAPI app unchanged, so local and AWS behave the same.
- **Secrets stay in SSM.** The Lambda role can read only `/livex-assistant/*`. Secrets are loaded at cold start, never stored in env vars or the repo.
- **Sessions live in DynamoDB.** Lambda can run several copies of the app, and a Confirm click may reach a different one than the chat turn that created it. Each request loads and saves the session (compressed JSON), so the confirmation gate holds.
- **Access code.** The URL can change a real calendar, so every `/api/*` call needs the code (`X-Passcode`). The UI asks for it once.
- **Cost.** About $0 at demo traffic: everything is pay-per-request, and it fits in the free tier.

**Deploy it yourself.** You need the AWS CLI, SAM CLI, and Python 3.11+. Docker is not needed: Linux wheels are downloaded directly.

1. Create the secrets once (CloudFormation can't create SecureStrings):
   ```bash
   for n in CAL_API_KEY ANTHROPIC_API_KEY ACCESS_PASSCODE; do
     aws ssm put-parameter --type SecureString --name /livex-assistant/$n --value REPLACE_ME
   done
   ```
   Then paste the real values in *Systems Manager → Parameter Store*.
2. `./deploy.sh`. This builds the bundle, deploys `template.yaml`, uploads the UI, and prints the URL.

Secrets are read at cold start, so after changing one, run `./deploy.sh` again to pick it up.

**Next steps for production:** Bedrock instead of the Claude API, so no AI key is needed (`LLM_PROVIDER=bedrock`, already supported; waiting on this account's model access). Cognito instead of a shared code. A custom domain. CloudWatch alarms on tool errors and latency. GitHub Actions with OIDC running `deploy.sh`.

## Known limitations

- **Only cal.com bookings.** "What's on my calendar" shows cal.com bookings, not other events on a connected Google Calendar. Those still block availability, because cal.com accounts for them in slots.
- **Single user.** One cal.com account and a shared access code. Locally, sessions are in memory and reset on restart.
- **Cold starts.** The first request after the Lambda has been idle takes about 10s while it starts the app and the MCP server.
- **No streaming.** Responses arrive whole after a few seconds; a typing indicator covers the wait.

## With more time

- **Streaming:** stream tokens and tool progress ("Checking Thursday…") over SSE.
- **Evals:** scripted multi-turn conversations against the fake cal.com, scored on tool sequence, no unconfirmed writes, and no invented times, run in CI.
- **Richer cards:** booking cards for list results, and recurring and seated event types.

## Layout

```
calcom_mcp/client.py    cal.com v2 client (per-endpoint API versions, readable errors)
calcom_mcp/server.py    MCP server: tools, annotations, guards
calcom_mcp/timeutil.py  timezone math and human-readable labels
app/agent.py            Claude tool loop + confirmation gate
app/llm.py              Claude API / Bedrock switch
app/main.py             FastAPI endpoints, MCP subprocess lifecycle
app/sessions.py         session storage: in-memory locally, DynamoDB on Lambda
app/static/             chat UI (vanilla JS, light/dark) + logo
template.yaml           AWS: CloudFront, S3, Lambda, DynamoDB, IAM (SAM)
deploy.sh, run.sh       build + deploy; Lambda entry point
tests/                  fake cal.com + scripted LLM; gate and guard tests
```
