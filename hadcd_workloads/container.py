"""Docker container task handler — runs a workload as an ephemeral
Docker container on the host executing the task.

This is the Phase 10a primitive that lets the user package any
workload (AI inference, video transcoding, rendering, scientific
compute) as a container, submit it through the normal task API,
and have it run on a heat-demanding node. The heat output per watt
is identical to any other workload — the dispatcher does not need
to distinguish container tasks from Python-function handlers.

Inputs/outputs in 10a are limited to what fits in the task JSON
payload (env vars, command, captured stdout/stderr up to 1 MiB).
Large blob I/O for inputs and outputs lands in Phase 10c. GPU
access (`--gpus all`) lands in Phase 10b.

The Docker daemon is reached via the host's socket
(`/var/run/docker.sock`) bind-mounted into the backend and agent
containers by `docker-compose.yml`. On Docker Desktop (Windows /
Mac) the socket is exposed by the host integration; on a Linux
host the socket is directly bind-mounted. Either way, the handler
talks to the same Docker daemon — sibling containers, not nested
("Docker out of Docker"). See `WORKLOAD_POLICY.md` §6 for the
policy / threat-model framing of socket access.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time

from hadcd_workloads.registry import register

logger = logging.getLogger("hadcd.workloads.container")

# Default timeout for container execution. Bounded so a runaway
# container cannot hold a worker forever; the local executor's
# runaway detection (Phase 4b) catches this at a higher level too.
DEFAULT_TIMEOUT_SEC = 600.0

# Hard upper bound on caller-specified timeout. 24h is comfortably
# more than any pilot workload should need; anything longer is a
# bug or misuse.
MAX_TIMEOUT_SEC = 86400.0

# Maximum captured log size per stream. 1 MiB each is enough to
# capture a meaningful traceback or summary without blowing up
# the task result row in the ledger.
MAX_LOG_BYTES = 1 * 1024 * 1024


# Prefix of the agent's blob-staging temp dirs (see Agent._run_assignment).
# Bind-mounts under <tempdir>/hadcd-blobs-* are agent-generated, never
# dispatcher-chosen, so they are always mountable.
_BLOB_STAGING_PREFIX = "hadcd-blobs-"

# Isolated bridge a hardened job lands on when its payload names no
# network. Matches the backend's CLIENT_SECURITY_PROFILE default.
_HARDENED_DEFAULT_NETWORK = "hadcd-client-bridge"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _is_within(path: str, parent: str) -> bool:
    """True if `path` resolves to `parent` or somewhere beneath it."""
    path = os.path.normcase(os.path.realpath(path))
    parent = os.path.normcase(os.path.realpath(parent))
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _check_mount_allowed(host_path: str) -> None:
    """Reject bind-mount sources the local host has not allowlisted.

    The mount policy is a *local* decision: the task payload arrives from
    the dispatcher, and a compromised or malicious dispatcher must not be
    able to mount arbitrary host paths (state dir, /etc, the Docker
    socket) into a container it also controls. Allowed sources are:

      * the agent's own blob-staging temp dirs (<tempdir>/hadcd-blobs-*),
        which the agent creates itself when staging task inputs/outputs;
      * directories under any prefix in HADCD_MOUNT_ALLOWLIST, an
        os.pathsep-separated list of absolute paths set by the host
        operator (never by the payload).

    Raises ValueError for anything else, failing the task cleanly.
    """
    tmp_root = os.path.realpath(tempfile.gettempdir())
    real = os.path.normcase(os.path.realpath(host_path))
    staging_root = os.path.normcase(tmp_root.rstrip(os.sep) + os.sep + _BLOB_STAGING_PREFIX)
    if real.startswith(staging_root):
        return
    for entry in os.environ.get("HADCD_MOUNT_ALLOWLIST", "").split(os.pathsep):
        entry = entry.strip()
        if entry and _is_within(host_path, entry):
            return
    raise ValueError(
        f"bind-mount source {host_path!r} is not allowlisted on this host — "
        "add its parent directory to HADCD_MOUNT_ALLOWLIST "
        "(CONTAINER_MOUNT_ALLOWLIST in the agent env) to permit it"
    )


class ContainerExecutionError(RuntimeError):
    """Raised when the container failed in a way the handler cannot
    recover from (image pull failure, daemon connection lost,
    timeout). The error message is the diagnostic; the original
    exception is chained as the cause."""


def _ensure_network(client, name: str) -> None:
    """Create the named user-defined bridge network if it does not exist.

    A user-defined bridge isolates the container from the host and from
    containers on other networks while still allowing outbound internet via
    NAT — matching the CLIENT_SECURITY_PROFILE intent. Built-in modes
    (bridge/host/none) always exist and are left untouched.
    """
    if name in ("bridge", "host", "none"):
        return
    from docker.errors import APIError, NotFound

    try:
        client.networks.get(name)
        return
    except NotFound:
        pass
    try:
        client.networks.create(name, driver="bridge")
        logger.info("created isolated bridge network %s", name)
    except APIError as exc:
        # A concurrent worker may have created it between our get and create;
        # tolerate that, but fail loudly if it's genuinely unavailable — a
        # client container must not silently fall back to an unisolated network.
        try:
            client.networks.get(name)
        except Exception:
            raise ContainerExecutionError(
                f"could not create isolated network {name}: {exc}"
            ) from exc


def _build_security_kwargs(client, args: dict) -> dict:
    """Translate an embedded security profile into docker-py run() kwargs.

    The profile travels in the payload, but the *floor* is enforced here,
    on the node: a ``hardened`` job always drops all capabilities, gains
    ``no-new-privileges``, runs unprivileged, and lands on an isolated
    bridge — even if the profile fields were stripped or loosened in
    transit. ``network_mode="host"`` is refused for every dispatched task.
    Setting HADCD_REQUIRE_HARDENED (CONTAINER_REQUIRE_HARDENED in the
    agent env) makes this node treat *every* container task as hardened,
    regardless of what the dispatcher says.

    Non-hardened operator tasks without a profile keep Docker defaults,
    as before (minus host networking).
    """
    kwargs: dict = {}
    hardened = bool(args.get("hardened")) or _env_true("HADCD_REQUIRE_HARDENED")

    cap_drop = args.get("cap_drop")
    if cap_drop:
        if not isinstance(cap_drop, list):
            raise ValueError("'cap_drop' must be a list of capability strings")
        kwargs["cap_drop"] = cap_drop

    # client_jobs embeds the list under 'security_opts' (plural); docker-py's
    # run() parameter is 'security_opt' (singular). Accept either key.
    security_opts = args.get("security_opts") or args.get("security_opt")
    if security_opts:
        if not isinstance(security_opts, list):
            raise ValueError("'security_opts' must be a list of strings")
        kwargs["security_opt"] = security_opts

    network_mode = args.get("network_mode")
    if network_mode:
        if not isinstance(network_mode, str):
            raise ValueError("'network_mode' must be a string")
        # Host networking exposes every host-bound service (including
        # loopback-only ones) to the container. No dispatched task gets it;
        # a workload that genuinely needs it is a host-config decision.
        if network_mode == "host":
            raise ValueError(
                "'network_mode' \"host\" is not permitted for dispatched "
                "container tasks"
            )
        _ensure_network(client, network_mode)
        kwargs["network"] = network_mode

    if hardened:
        kwargs["privileged"] = False
        # The floor overrides, not merges: ALL is a superset of any
        # payload-supplied cap_drop list.
        kwargs["cap_drop"] = ["ALL"]
        opts = list(kwargs.get("security_opt") or [])
        if not any(str(o).startswith("no-new-privileges") for o in opts):
            opts.append("no-new-privileges:true")
        kwargs["security_opt"] = opts
        if "network" not in kwargs:
            _ensure_network(client, _HARDENED_DEFAULT_NETWORK)
            kwargs["network"] = _HARDENED_DEFAULT_NETWORK

    return kwargs


@register("container")
def _container(args: dict) -> dict:
    """Pull and run a Docker image; capture stdout, stderr, exit code.

    Required args:
        image (str): the image reference, e.g. ``"busybox:latest"``

    Optional args:
        command (str | list[str]): the command to run inside the
            container. If absent, the image's default ENTRYPOINT /
            CMD runs.
        env (dict[str, str]): environment variables to pass into the
            container. Values are coerced to strings.
        timeout_sec (float): max seconds to wait for completion.
            Default 600 (10 min). Capped at MAX_TIMEOUT_SEC.
        gpu_request (bool): if True, the container gets access to all
            NVIDIA GPUs on the host (equivalent to `docker run
            --gpus all`). Requires NVIDIA Container Toolkit to be
            installed on the host; without it, Docker will refuse to
            create the container. Default False — CPU only.
        volumes (list[dict]): Phase 10c — bind-mounts to inject into
            the container. Each entry must have:
                host_path (str): absolute path on the Docker host.
                container_path (str): absolute path inside the container.
                mode (str, optional): ``"ro"`` (default) or ``"rw"``.
            Example:
                [{"host_path": "/blobs/in/audio.wav",
                  "container_path": "/input/audio.wav",
                  "mode": "ro"}]
        output_dir (str): Phase 10c — absolute path on the Docker host
            where the container should write its output files. Mounted
            as ``/output`` inside the container in ``rw`` mode. After
            the container exits the handler lists the files it finds
            there and includes them in ``output_files`` in the result.
            Set by the agent when it has staged an output directory for
            blob upload.

    Returns:
        dict with:
            exit_code (int): the container's exit status
            stdout (str): captured stdout, truncated to MAX_LOG_BYTES
            stderr (str): captured stderr, truncated to MAX_LOG_BYTES
            image (str): the image that was run (echoed back)
            command: the command argument (echoed back, may be None)
            duration_sec (float): wall-clock seconds from start to
                completion
            output_files (list[str]): paths on the host of files found
                in ``output_dir`` after the container exits (empty list
                if ``output_dir`` was not set).

    A non-zero exit code is *not* an exception — the handler returns
    normally and the caller decides what to do with it. Genuine
    handler failures (daemon unreachable, image unpullable, wait
    timeout) raise ContainerExecutionError; invalid args raise
    ValueError. Both surface to the worker as task-level failures.
    """
    # Lazy import so a worker that never runs a container task does
    # not pay the import cost — and so module load does not break in
    # environments without the docker SDK installed (e.g. some tests).
    try:
        import docker
        from docker.errors import APIError, DockerException, ImageNotFound
    except ImportError as exc:
        raise ContainerExecutionError(
            "docker Python SDK is not installed. Add 'docker' to the "
            "host service's requirements.txt and rebuild."
        ) from exc

    image = args.get("image")
    if not image or not isinstance(image, str):
        raise ValueError("'image' (str) is required")

    command = args.get("command")
    if command is not None and not isinstance(command, (str, list)):
        raise ValueError("'command' must be a string or list of strings")

    env = args.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("'env' must be a dict")
    env = {str(k): str(v) for k, v in env.items()}

    try:
        timeout_sec = float(args.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    except (TypeError, ValueError) as exc:
        raise ValueError("'timeout_sec' must be a number") from exc
    if timeout_sec <= 0 or timeout_sec > MAX_TIMEOUT_SEC:
        raise ValueError(
            f"'timeout_sec' must be between 0 and {MAX_TIMEOUT_SEC:.0f}"
        )

    gpu_request = bool(args.get("gpu_request", False))

    # Phase 10c — volumes
    raw_volumes = args.get("volumes") or []
    if not isinstance(raw_volumes, list):
        raise ValueError("'volumes' must be a list of {host_path, container_path, mode?}")
    volumes_dict: dict = {}
    for i, v in enumerate(raw_volumes):
        if not isinstance(v, dict):
            raise ValueError(f"volumes[{i}] must be a dict")
        host_path = v.get("host_path")
        container_path = v.get("container_path")
        if not host_path or not isinstance(host_path, str):
            raise ValueError(f"volumes[{i}].host_path (str) is required")
        if not container_path or not isinstance(container_path, str):
            raise ValueError(f"volumes[{i}].container_path (str) is required")
        mode = v.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise ValueError(f"volumes[{i}].mode must be 'ro' or 'rw'")
        # Local mount policy — the payload never decides what is mountable.
        _check_mount_allowed(host_path)
        volumes_dict[host_path] = {"bind": container_path, "mode": mode}

    # Phase 10c — output_dir: mount as /output rw, collect files after run
    output_dir = args.get("output_dir")
    if output_dir is not None:
        if not isinstance(output_dir, str):
            raise ValueError("'output_dir' must be a string path")
        _check_mount_allowed(output_dir)
        volumes_dict[output_dir] = {"bind": "/output", "mode": "rw"}

    try:
        client = docker.from_env()
    except DockerException as exc:
        raise ContainerExecutionError(
            f"could not connect to Docker daemon: {exc}"
        ) from exc

    logger.info(
        "container start: image=%s command=%r timeout=%.0fs",
        image,
        command,
        timeout_sec,
    )

    # Phase 10b — translate gpu_request=True into Docker's
    # device_requests, equivalent to `docker run --gpus all`. count=-1
    # means "all GPUs visible to the daemon"; capabilities=[['gpu']]
    # is the NVIDIA Container Toolkit's hook. Skipping this for the
    # default CPU-only case keeps the daemon call identical to 10a.
    device_requests = None
    if gpu_request:
        from docker.types import DeviceRequest

        device_requests = [
            DeviceRequest(count=-1, capabilities=[["gpu"]])
        ]

    # Container hardening (Phase 18b). Client-submitted jobs embed a security
    # profile (cap_drop / security_opts / network_mode / hardened) so untrusted
    # containers run locked down. These kwargs are applied ONLY when present —
    # the operator's own in-network container tasks omit them and run with
    # Docker defaults, as before. This is the enforcement point: without it the
    # profile is inert and every client container runs with full privileges.
    security_kwargs = _build_security_kwargs(client, args)

    start = time.monotonic()
    container = None
    try:
        try:
            container = client.containers.run(
                image=image,
                command=command,
                environment=env,
                device_requests=device_requests,
                volumes=volumes_dict or None,
                detach=True,
                # We remove explicitly in the finally block so logs are
                # capturable after wait() returns.
                auto_remove=False,
                **security_kwargs,
            )
        except ImageNotFound as exc:
            raise ContainerExecutionError(
                f"image not found: {image}"
            ) from exc
        except APIError as exc:
            raise ContainerExecutionError(
                f"docker API error starting container: {exc}"
            ) from exc

        try:
            wait_result = container.wait(timeout=timeout_sec)
        except Exception as exc:
            # Best-effort kill so a hung container doesn't keep
            # consuming resources after we've given up on it.
            try:
                container.kill()
            except Exception:
                logger.exception("container kill failed after wait error")
            raise ContainerExecutionError(
                f"container did not finish within {timeout_sec:.0f}s: {exc}"
            ) from exc

        exit_code = int(wait_result.get("StatusCode", -1))
        duration_sec = time.monotonic() - start

        try:
            stdout_bytes = container.logs(stdout=True, stderr=False)[
                :MAX_LOG_BYTES
            ]
            stderr_bytes = container.logs(stdout=False, stderr=True)[
                :MAX_LOG_BYTES
            ]
        except APIError as exc:
            logger.warning("container log capture failed: %s", exc)
            stdout_bytes = b""
            stderr_bytes = b""

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Phase 10c — collect files the container wrote to output_dir.
        output_files: list[str] = []
        if output_dir:
            import os as _os
            try:
                for fname in sorted(_os.listdir(output_dir)):
                    fpath = _os.path.join(output_dir, fname)
                    if _os.path.isfile(fpath):
                        output_files.append(fpath)
            except OSError as exc:
                logger.warning("could not list output_dir %s: %s", output_dir, exc)

        logger.info(
            "container done: image=%s exit_code=%d duration=%.2fs "
            "stdout=%d bytes stderr=%d bytes output_files=%d",
            image,
            exit_code,
            duration_sec,
            len(stdout_bytes),
            len(stderr_bytes),
            len(output_files),
        )

        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "image": image,
            "command": command,
            "duration_sec": duration_sec,
            "gpu_request": gpu_request,
            "output_files": output_files,
        }
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.exception(
                    "container cleanup failed (image=%s)", image
                )
