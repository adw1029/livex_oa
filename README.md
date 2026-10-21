# LiveX.AI Assistant

A chat assistant for your cal.com calendar. Type "book a 30-min intro with a candidate Thursday afternoon" and it
finds open times and books it. It can also list, reschedule, update and cancel meetings.

**Live demo:** https://d8f0ss1s0decm.cloudfront.net (the access code is in my submission email)

![The assistant showing open times and a booking confirmation](docs/screenshot.png)

Nothing changes on the calendar until you click confirm. Bookings are real, so use an email that can receive mail.

## Run it locally

You need Python 3.11+, a cal.com API key and an Anthropic API key.

```bash
make install
cp .env.example .env    # add CAL_API_KEY and ANTHROPIC_API_KEY
make run                # http://localhost:8000
make test
```

## How it works

```
Browser ──▶ FastAPI + Claude ──MCP──▶ cal.com MCP server ──▶ cal.com API
```

- The cal.com tools are a standalone MCP server, so they also work in Claude Desktop or Claude Code.
- Every change shows a confirmation card first. The code enforces this, not the prompt.
- Python does the timezone math, not the model. Only times that cal.com actually returned are shown.

It runs on AWS: CloudFront, S3 and Lambda, with secrets in SSM and chat sessions in DynamoDB. To deploy your
own, add `CAL_API_KEY`, `ANTHROPIC_API_KEY` and `ACCESS_PASSCODE` to SSM under `/livex-assistant/`, then run
`make deploy`.

## What I'd do next

- Stream replies instead of waiting for the whole answer.
- Support multiple users with their own sign-in.
- Add evals that run in CI.
