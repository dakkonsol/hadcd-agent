"""Agent command-line entry point.

    python -m agent run

Inside docker-compose it's just a service:

    docker compose --profile agent up agent

The whole configuration is environment-driven (see `agent/config.py`)
so this CLI is intentionally tiny — there is only the `run`
subcommand and it builds an Agent from the env and starts it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pydantic import ValidationError

from agent.agent import Agent
from agent.config import AgentSettings
from agent.heat_source import BMSConfigError, build_source
from agent.state import AgentState
from agent.provisioner import provision
from agent.wifi_provision import provision as wifi_provision


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the agent against the configured backend")
    sub.add_parser(
        "wifi-provision",
        help=(
            "run the one-time WiFi captive-portal provisioner "
            "(no-op if ethernet is connected or WiFi is already configured)"
        ),
    )
    sub.add_parser(
        "provision",
        help=(
            "run the phone-friendly HADCD node configuration wizard on port 8080 "
            "(no-op if the node is already configured)"
        ),
    )
    args = parser.parse_args()

    if args.command == "wifi-provision":
        sys.exit(wifi_provision())

    if args.command == "provision":
        sys.exit(provision())

    if args.command == "run":
        try:
            settings = AgentSettings()
        except ValidationError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            sys.exit(2)

        # Validate BMS settings up-front so a misconfigured heat
        # source fails at startup, not later in the demand loop.
        try:
            build_source(settings).aclose  # noqa: B018 — just exercise the factory
        except BMSConfigError as exc:
            print(f"BMS config error: {exc}", file=sys.stderr)
            sys.exit(2)

        state = AgentState(settings.agent_state_file)
        state.load()  # tolerates missing/malformed; agent will enroll fresh

        if not state.enrolled and not settings.enrollment_token:
            print(
                "error: no persisted identity at "
                f"{settings.agent_state_file} and no enrollment token. "
                "Set ENROLLMENT_TOKENS to allow first-time enrollment.",
                file=sys.stderr,
            )
            sys.exit(2)

        asyncio.run(_run(settings, state))


async def _run(settings: AgentSettings, state: AgentState) -> None:
    agent = Agent(settings, state)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Signal-based shutdown only works on POSIX. On Windows the agent
    # is expected to run under a service manager that signals via the
    # process; Ctrl+C in a terminal works regardless.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await agent.run(stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
