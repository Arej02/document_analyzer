# Agent 3 sandbox runner (design_agent3.md §7).
# Build: docker build -t galvanise-agent3-runner:0.1 backend/agent3_runner
# No USER / ENTRYPOINT here: sandbox.py sets the user, limits and command on `docker run`.
FROM python:3.14-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
