"""One-time interactive Ecobee OAuth PIN-flow setup for the agent.

Run once per pilot host:

    python -m agent.ecobee_setup

It walks the operator through Ecobee's PIN authorisation, picks a
thermostat, and writes:

    {
      "refresh_token": "...",
      "thermostat_id": "..."
    }

to the configured `ECOBEE_STATE_FILE` (default
`/var/lib/hadcd-agent/ecobee_state.json`). After that, the agent's
EcobeeHeatSource adapter handles token refresh autonomously — Ecobee
rotates refresh tokens on every use, and the adapter persists each
new one to the same state file atomically.

Prerequisite: a developer "app" registered at
https://www.ecobee.com/developers/ with the `smartRead` scope. That
registration produces the API key (a.k.a. client ID) you'll enter
when prompted. Registration is free and instantaneous.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://api.ecobee.com/authorize"
TOKEN_URL = "https://api.ecobee.com/token"
THERMOSTAT_URL = "https://api.ecobee.com/1/thermostat"

# Where the agent persists Ecobee state by default. Overridden by
# ECOBEE_STATE_FILE if set in the environment, mirroring the agent's
# config.py resolution order.
DEFAULT_STATE_FILE = "/var/lib/hadcd-agent/ecobee_state.json"


def _prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{message}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    return raw


def _request_pin(client: httpx.Client, api_key: str) -> dict:
    """Step 1 of Ecobee's PIN flow — exchange the API key for a PIN."""
    resp = client.get(
        AUTHORIZE_URL,
        params={
            "response_type": "ecobeePin",
            "client_id": api_key,
            "scope": "smartRead",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _poll_for_token(
    client: httpx.Client,
    api_key: str,
    ecobee_pin_code: str,
    timeout_sec: float = 600,
) -> dict:
    """Step 3 — after the user enters the PIN at ecobee.com, exchange
    the auth code for access + refresh tokens. Polls until success or
    the user-facing expiry runs out."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        resp = client.post(
            TOKEN_URL,
            params={
                "grant_type": "ecobeePin",
                "code": ecobee_pin_code,
                "client_id": api_key,
            },
        )
        if resp.status_code == 200:
            return resp.json()
        # 400 with error="authorization_pending" is the expected pre-PIN-entry
        # response; anything else is fatal.
        if resp.status_code == 400:
            try:
                err = resp.json().get("error")
            except ValueError:
                err = None
            if err == "authorization_pending":
                time.sleep(5)
                continue
        resp.raise_for_status()
    raise TimeoutError(
        "User did not enter the PIN within the allotted time. Re-run setup."
    )


def _list_thermostats(client: httpx.Client, access_token: str) -> list[dict]:
    """Discover the thermostats this account can read."""
    selection = json.dumps(
        {
            "selection": {
                "selectionType": "registered",
                "selectionMatch": "",
                "includeBasic": True,
            }
        },
        separators=(",", ":"),
    )
    resp = client.get(
        THERMOSTAT_URL,
        params={"json": selection},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    resp.raise_for_status()
    return resp.json().get("thermostatList", []) or []


def _write_state_file(
    state_file: Path, refresh_token: str, thermostat_id: str
) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "refresh_token": refresh_token,
                "thermostat_id": thermostat_id,
            },
            indent=2,
        )
    )
    tmp.replace(state_file)
    try:
        # 0600 — only the owning user should read this; refresh tokens
        # are effectively credentials.
        os.chmod(state_file, 0o600)
    except OSError:
        # Best-effort: the chmod may fail on Windows or some volumes.
        pass


def main() -> int:
    print("=" * 60)
    print("HADCD — Ecobee adapter one-time setup")
    print("=" * 60)
    print()
    print(
        "This walks you through Ecobee's PIN-based OAuth flow and "
        "persists\nthe refresh token so the agent can run unattended "
        "afterward."
    )
    print()
    print(
        "Prerequisite: a developer app registered at "
        "https://www.ecobee.com/developers/\nwith scope `smartRead`. "
        "You'll need its API key (client ID)."
    )
    print()

    api_key = _prompt("Ecobee API key (client ID)")
    if not api_key:
        print("API key is required. Aborting.", file=sys.stderr)
        return 2

    state_file_str = _prompt(
        "State-file path",
        default=os.environ.get("ECOBEE_STATE_FILE", DEFAULT_STATE_FILE),
    )
    state_file = Path(state_file_str)

    with httpx.Client(timeout=15.0) as client:
        try:
            pin_resp = _request_pin(client, api_key)
        except httpx.HTTPError as exc:
            print(f"Failed to request a PIN: {exc}", file=sys.stderr)
            return 2

        pin = pin_resp.get("ecobeePin")
        code = pin_resp.get("code")
        if not pin or not code:
            print(
                f"Unexpected PIN response shape: {pin_resp}",
                file=sys.stderr,
            )
            return 2

        print()
        print("=" * 60)
        print(f"  Your PIN is: {pin}")
        print("=" * 60)
        print()
        print(
            "Go to https://www.ecobee.com/consumerportal/index.html, "
            "sign in,\nopen the menu (top-right), choose 'My Apps' -> "
            "'Add Application',\nand enter the PIN above. Click "
            "'Validate', then 'Add Application'."
        )
        print()
        input("Press Enter once you've added the application... ")

        try:
            token_resp = _poll_for_token(client, api_key, code)
        except (httpx.HTTPError, TimeoutError) as exc:
            print(f"Token exchange failed: {exc}", file=sys.stderr)
            return 2

        access_token = token_resp.get("access_token")
        refresh_token = token_resp.get("refresh_token")
        if not access_token or not refresh_token:
            print(
                f"Unexpected token response: {token_resp}",
                file=sys.stderr,
            )
            return 2

        print()
        print("OAuth successful. Discovering thermostats...")
        try:
            thermostats = _list_thermostats(client, access_token)
        except httpx.HTTPError as exc:
            print(f"Could not list thermostats: {exc}", file=sys.stderr)
            return 2

        if not thermostats:
            print(
                "No thermostats found on this account. Aborting.",
                file=sys.stderr,
            )
            return 2

        print()
        print("Thermostats registered to this account:")
        for idx, t in enumerate(thermostats, start=1):
            print(
                f"  {idx}. id={t.get('identifier')} "
                f"name={t.get('name')!r}"
            )
        print()

        if len(thermostats) == 1:
            chosen = thermostats[0]
            print(
                f"Auto-selecting the only thermostat: "
                f"{chosen.get('identifier')}"
            )
        else:
            while True:
                pick = _prompt(f"Pick one (1-{len(thermostats)})")
                try:
                    chosen = thermostats[int(pick) - 1]
                    break
                except (ValueError, IndexError):
                    print("Invalid selection.")

        thermostat_id = chosen.get("identifier")
        if not thermostat_id:
            print("Selected thermostat has no identifier.", file=sys.stderr)
            return 2

    _write_state_file(state_file, refresh_token, thermostat_id)

    print()
    print(f"Wrote {state_file}")
    print()
    print("Next steps:")
    print(f"  1. Set BMS_SOURCE=ecobee in your agent config")
    print(f"  2. Set ECOBEE_API_KEY={api_key}")
    print(f"  3. Set ECOBEE_THERMOSTAT_ID={thermostat_id}")
    print(f"  4. Set ECOBEE_STATE_FILE={state_file}")
    print(f"  5. Set ECOBEE_DEMAND_WHEN_HEATING_KW to the kW value to")
    print(f"     report when the thermostat is calling for heat.")
    print(f"  6. Restart the agent.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
