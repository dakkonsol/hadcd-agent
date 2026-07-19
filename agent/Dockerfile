FROM python:3.11-slim

WORKDIR /app

# The `agent` package lives at /app/agent so `python -m agent` works.
# `hadcd_workloads` is bind-mounted alongside at /app/hadcd_workloads
# in docker-compose; the COPY here makes the image self-contained for
# any non-compose deployment (e.g. running the image directly).
COPY . /app/agent/
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

# Where the agent keeps its persisted identity and reads BMS data.
# Bind-mount or volume-mount this directory in production deployments.
RUN mkdir -p /var/lib/hadcd-agent
VOLUME ["/var/lib/hadcd-agent"]

ENTRYPOINT ["python", "-m", "agent"]
CMD ["run"]
