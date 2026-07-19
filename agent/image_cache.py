"""Docker image cache management (Phase 10d).

Two responsibilities:

Pre-pull
--------
At agent startup, pull every image listed in ``DOCKER_PREPULL_IMAGES``
that is not already present on the Docker host. AI images (Whisper,
Ollama, Stable Diffusion) are 3-20 GB; on a home connection the first
pull of a cold image can take 10-30 minutes and makes the first task
appear to hang. Pre-pulling at startup amortises that cost while the
node is otherwise idle.

Disk-budget GC
--------------
After each container task completes, check the total disk space used
by Docker image layers. If it exceeds the configured budget
(``DOCKER_IMAGE_BUDGET_GB``), prune:

  1. Dangling images first (untagged intermediate layers — always
     safe to remove, recover space quickly).
  2. Unused images (tagged, not referenced by any running or stopped
     container) only if still over budget after dangling pruning.

The GC is best-effort — if Docker is unreachable or the prune fails,
a warning is logged and the agent continues normally.

Both operations use the Docker SDK synchronously. Call them from a
``loop.run_in_executor`` thread to avoid blocking the asyncio event
loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid importing docker at module level

logger = logging.getLogger("hadcd.agent.image_cache")

# Bytes per GiB — used when converting the budget setting.
_GIB = 1024 ** 3


class ImageCacheManager:
    """Pre-pull and GC Docker images on the agent host.

    Parameters
    ----------
    prepull_images:
        List of ``image:tag`` references to pull at startup.
        Empty list → no pre-pull.
    budget_gb:
        Maximum GiB of image layers before GC runs.
        ``0`` disables GC.
    """

    def __init__(
        self,
        prepull_images: list[str],
        budget_gb: float,
    ) -> None:
        self._prepull = [img.strip() for img in prepull_images if img.strip()]
        self._budget_bytes = int(budget_gb * _GIB) if budget_gb > 0 else 0

    # ------------------------------------------------------------------
    # Public API (synchronous — run in executor thread)
    # ------------------------------------------------------------------

    def prepull_all(self) -> None:
        """Pull every configured image that is not already cached.

        Skips images already present (avoids redundant network I/O).
        Logs a warning and continues if any individual pull fails —
        a failed pre-pull is not fatal; the task will cold-pull when
        it eventually runs.
        """
        if not self._prepull:
            return

        try:
            import docker
            from docker.errors import DockerException
        except ImportError:
            logger.warning(
                "docker SDK not installed — skipping image pre-pull"
            )
            return

        try:
            client = docker.from_env()
        except DockerException as exc:
            logger.warning("pre-pull: cannot connect to Docker daemon: %s", exc)
            return

        # Build a set of already-present repo:tag strings for fast lookup.
        present = _present_image_tags(client)

        for image_ref in self._prepull:
            if image_ref in present:
                logger.debug("pre-pull: already cached %s", image_ref)
                continue
            logger.info("pre-pull: pulling %s …", image_ref)
            try:
                client.images.pull(image_ref)
                logger.info("pre-pull: pulled  %s", image_ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pre-pull: failed to pull %s (will cold-pull on first use): %s",
                    image_ref,
                    exc,
                )

    def maybe_gc(self) -> None:
        """Prune images if total layer usage exceeds the configured budget.

        Does nothing when budget is 0 (GC disabled). Safe to call
        frequently — the budget check is a fast local API call.
        """
        if self._budget_bytes == 0:
            return

        try:
            import docker
            from docker.errors import DockerException
        except ImportError:
            return  # no SDK → nothing to GC

        try:
            client = docker.from_env()
        except DockerException as exc:
            logger.warning("image GC: cannot connect to Docker daemon: %s", exc)
            return

        try:
            usage = _total_image_bytes(client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("image GC: could not measure image disk usage: %s", exc)
            return

        if usage <= self._budget_bytes:
            logger.debug(
                "image GC: %.1f GiB used, %.1f GiB budget — no action",
                usage / _GIB,
                self._budget_bytes / _GIB,
            )
            return

        logger.info(
            "image GC: %.1f GiB used > %.1f GiB budget — pruning dangling images",
            usage / _GIB,
            self._budget_bytes / _GIB,
        )
        try:
            result = client.images.prune(filters={"dangling": True})
            reclaimed = result.get("SpaceReclaimed", 0)
            logger.info(
                "image GC: dangling prune reclaimed %.1f MiB",
                reclaimed / (1024 ** 2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("image GC: dangling prune failed: %s", exc)
            return

        # Re-measure after dangling prune.
        try:
            usage = _total_image_bytes(client)
        except Exception:  # noqa: BLE001
            return

        if usage <= self._budget_bytes:
            return

        logger.info(
            "image GC: still %.1f GiB > budget — pruning unused images",
            usage / _GIB,
        )
        try:
            result = client.images.prune(filters={"dangling": False})
            reclaimed = result.get("SpaceReclaimed", 0)
            logger.info(
                "image GC: unused prune reclaimed %.1f MiB",
                reclaimed / (1024 ** 2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("image GC: unused prune failed: %s", exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _present_image_tags(client) -> set[str]:
    """Return the set of ``repo:tag`` strings for all locally cached images."""
    tags: set[str] = set()
    try:
        for img in client.images.list():
            for tag in (img.tags or []):
                tags.add(tag)
    except Exception:  # noqa: BLE001
        pass
    return tags


def _total_image_bytes(client) -> int:
    """Return total bytes used by all image layers via ``docker system df``."""
    try:
        df = client.df()
        # ``Images`` is a list of image-usage dicts; ``Size`` is layer size,
        # ``SharedSize`` is size shared with other images.
        total = sum(
            img.get("Size", 0) for img in (df.get("Images") or [])
        )
        return int(total)
    except Exception:  # noqa: BLE001
        # Fall back to summing image.attrs["Size"] if df() is unavailable.
        total = 0
        for img in client.images.list():
            total += img.attrs.get("Size", 0)
        return total


def build_image_cache(settings) -> ImageCacheManager:
    """Construct an ``ImageCacheManager`` from ``AgentSettings``."""
    images = [
        img.strip()
        for img in settings.docker_prepull_images.split(",")
        if img.strip()
    ]
    return ImageCacheManager(
        prepull_images=images,
        budget_gb=settings.docker_image_budget_gb,
    )
