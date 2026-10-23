#!/bin/bash
# Lambda entry point: Lambda Web Adapter forwards each invocation to this server.
# The adapter skips the Python runtime's bootstrap, so add the dependency layer to the path here.
export PYTHONPATH="/opt/python${PYTHONPATH:+:$PYTHONPATH}"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
