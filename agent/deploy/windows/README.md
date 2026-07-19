# Installing the HADCD agent as a Windows service

A walkthrough for installing the agent as a Windows service via
[NSSM](https://nssm.cc/) (the Non-Sucking Service Manager — the
standard wrapper for running non-Windows-native programs as
services). For the end-to-end *operator* flow (choosing values,
getting an enrollment token, etc.), see
[docs/deployment.md](../../../docs/deployment.md). This file is the
Windows-specific subroutine.

Tested on Windows Server 2019/2022 and Windows 10/11. The provided
PowerShell script auto-downloads NSSM 2.24 if it is not on PATH —
no system-wide NSSM install is required.

## Path mapping (Linux → Windows)

The agent's defaults assume a Linux deployment. On Windows the
equivalent locations are:

| Concept           | Linux default                 | Windows default                                   |
|-------------------|-------------------------------|---------------------------------------------------|
| Agent code        | `/opt/hadcd-agent`            | `%ProgramFiles%\hadcd-agent`                      |
| Env file          | `/etc/hadcd-agent/agent.env`  | `%ProgramData%\hadcd-agent\agent.env`             |
| State directory   | `/var/lib/hadcd-agent`        | `%ProgramData%\hadcd-agent`                       |
| Logs              | `journalctl -u hadcd-agent`   | `%ProgramData%\hadcd-agent\logs\agent.out.log` (rotated) |
| State file        | `/var/lib/hadcd-agent/state.json` | `%ProgramData%\hadcd-agent\state.json`            |
| BMS file (file mode) | `/var/lib/hadcd-agent/bms.json` | `%ProgramData%\hadcd-agent\bms.json`           |
| Service user      | `hadcd-agent` (system user)   | `LocalSystem` (default) or a dedicated service account |

The env file's `AGENT_STATE_FILE` and `BMS_FILE` must be set to the
Windows paths above (the template defaults to Linux paths).

## Steps

### 1. Install Python 3.11+

Download from <https://www.python.org/downloads/>. During install
tick **"Add python.exe to PATH"**. Verify:

```powershell
python --version    # 3.11.x or newer
```

### 2. Copy the agent code

```powershell
$dst = "$env:ProgramFiles\hadcd-agent"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item -Recurse <path-to-checkout>\agent           $dst\
Copy-Item -Recurse <path-to-checkout>\hadcd_workloads $dst\
```

Only the `agent` and `hadcd_workloads` directories are needed at
runtime; everything else is server-side.

### 3. Create the venv and install dependencies

```powershell
python -m venv "$env:ProgramFiles\hadcd-agent\.venv"
& "$env:ProgramFiles\hadcd-agent\.venv\Scripts\pip" install `
    -r "$env:ProgramFiles\hadcd-agent\agent\requirements.txt"
```

### 4. Drop in the env file

```powershell
New-Item -ItemType Directory -Force "$env:ProgramData\hadcd-agent" | Out-Null
Copy-Item "$env:ProgramFiles\hadcd-agent\agent\config.env.example" `
          "$env:ProgramData\hadcd-agent\agent.env"
notepad   "$env:ProgramData\hadcd-agent\agent.env"
```

At a minimum edit `HADCD_API`, `ENROLLMENT_TOKENS`, `NODE_NAME`,
`MAX_POWER_KW` — and change `AGENT_STATE_FILE` / `BMS_FILE` from
the Linux defaults to:

```
AGENT_STATE_FILE=C:\ProgramData\hadcd-agent\state.json
BMS_FILE=C:\ProgramData\hadcd-agent\bms.json
```

Restrict permissions so non-admins cannot read the bearer tokens
(adjust the user list to your environment):

```powershell
icacls "$env:ProgramData\hadcd-agent\agent.env" `
  /inheritance:r /grant:r "SYSTEM:R" "Administrators:F"
```

### 5. Register the service

From an **elevated** PowerShell prompt:

```powershell
& "$env:ProgramFiles\hadcd-agent\agent\deploy\windows\install-service.ps1"
```

The script:
- locates NSSM (PATH first; downloads 2.24 to
  `%ProgramData%\hadcd-agent\nssm\` if absent);
- stops & removes any pre-existing `hadcd-agent` service;
- registers the service to run `python.exe -m agent run`;
- loads the env file's variables into the service environment;
- configures stdout/stderr to rotated files under
  `%ProgramData%\hadcd-agent\logs\` (10 MiB cap per file);
- sets a restart-on-failure policy throttled to one restart per 5s;
- starts the service.

Verify:

```powershell
Get-Service hadcd-agent
Get-Content -Wait -Tail 50 "$env:ProgramData\hadcd-agent\logs\agent.out.log"
```

A successful first start logs `enrolled as node <UUID>`. Subsequent
restarts log `resuming as node <UUID>`.

## Platform-specific quirks

- **`LocalSystem` vs a dedicated account.** The default service
  user (`LocalSystem`) can read `%ProgramFiles%` and write
  `%ProgramData%`. If your security policy requires a non-system
  account, create one with `Log on as a service` rights and
  uncomment the `ObjectName` line in `install-service.ps1`.
- **Windows Event Log.** NSSM does **not** write to the Event Log by
  default — the agent's stdout/stderr go to the rotated files only.
  If your monitoring expects Event Log entries, point your log
  collector at the log files, or add a separate `winlogbeat` /
  similar collector.
- **Path separators in the env file.** Use Windows backslashes
  (`C:\ProgramData\hadcd-agent\state.json`) — Python's `pathlib`
  handles them transparently and they match what Windows users see.
- **Antivirus.** Some endpoint-protection products flag NSSM as a
  generic-service-wrapper indicator. Whitelist the NSSM path under
  `%ProgramData%\hadcd-agent\nssm\` if needed.
- **Reboots / power loss.** State writes are atomic
  (`tempfile + os.replace`), so an ill-timed reboot cannot corrupt
  the identity.

## Upgrades

```powershell
Stop-Service hadcd-agent
# Replace agent\ and hadcd_workloads\ under %ProgramFiles%\hadcd-agent.
& "$env:ProgramFiles\hadcd-agent\.venv\Scripts\pip" install `
    -r "$env:ProgramFiles\hadcd-agent\agent\requirements.txt"
Start-Service hadcd-agent
```

The agent's identity survives — only the code is replaced.

## Decommissioning

```powershell
Stop-Service hadcd-agent
nssm remove hadcd-agent confirm    # use the path from install-service.ps1 if not on PATH
Remove-Item -Recurse "$env:ProgramFiles\hadcd-agent"
Remove-Item -Recurse "$env:ProgramData\hadcd-agent"
```

After this the central server still has the node's row marked
online (until heartbeats lapse and Layer 1 marks it offline) — the
central operator should remove the row in the next maintenance pass.
