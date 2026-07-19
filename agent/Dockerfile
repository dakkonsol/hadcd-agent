# Build from the REPOSITORY ROOT so both packages are in context:
#
#   docker build -f agent/Dockerfile -t hadcd-agent .
#
# The agent imports `hadcd_workloads` as a sibling top-level package, so
# both `agent/` and `hadcd_workloads/` must land side by side on the
# Python path. Building from agent/ alone silently omits the workloads
# package and the container fails at import time — hence the root context.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so the layer caches across source edits.
COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

# Both packages, laid out as importable top-level packages on /app.
COPY agent /app/agent
COPY hadcd_workloads /app/hadcd_workloads

# `python -m agent` resolves because /app is the working dir and both
# packages sit directly under it.
ENV PYTHONPATH=/app

# Where the agent keeps its persisted identity and reads BMS data.
# Bind-mount or volume-mount this directory in production deployments.
RUN mkdir -p /var/lib/hadcd-agent
VOLUME ["/var/lib/hadcd-agent"]

ENTRYPOINT ["python", "-m", "agent"]
CMD ["run"]
