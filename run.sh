#!/bin/bash
# Lambda entry point: Lambda Web Adapter forwards each invocation to this server.
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
