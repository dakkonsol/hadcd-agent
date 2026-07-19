"""HTTP client helpers for blob upload / download (Phase 10c).

The agent uses this module to:
  * Download input blobs from the HADCD backend before running a
    container task (so they can be bind-mounted into the container).
  * Upload output blobs to the HADCD backend after a container task
    finishes (so the operator can fetch the results).

All calls go to the same httpx.AsyncClient that the main agent loop
uses for heartbeats and work polling, so connections are reused and the
agent's single bearer token is the credential.

The blob directory on the agent host (BLOB_STORAGE_DIR) is where
downloaded files land and where the agent reads output files before
uploading them. It is also the directory that the container handler
passes to Docker as a bind-mount source — so the container and the
agent can share files through the host filesystem without copying data
through the Docker API.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

import httpx

logger = logging.getLogger("hadcd.agent.blobs")

# Chunk size for streaming downloads to disk.
_DOWNLOAD_CHUNK = 65536


class BlobClientError(RuntimeError):
    """Raised when a blob upload or download fails non-transiently."""


def safe_blob_name(name: str | None, fallback: str) -> str:
    """Reduce a server-supplied blob filename to a safe single-segment basename.

    The agent turns this name into a filesystem path (a download destination
    and a Docker bind-mount source). A traversal sequence would let a caller
    write to, or mount, an arbitrary host path. Strip all directory components
    (both separators, so a Windows agent is covered too); fall back to the blob
    id when nothing usable remains.
    """
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not base or base in (".", ".."):
        return fallback
    return base


class BlobClient:
    """Upload and download blobs via the HADCD HTTP API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        node_token: str,
        blob_storage_dir: str,
    ) -> None:
        self._client = client
        self._auth = {"Authorization": f"Bearer {node_token}"}
        self._dir = Path(blob_storage_dir)

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(self, blob_id: str, dest_path: Path | str) -> Path:
        """Download blob *blob_id* and write it to *dest_path*.

        *dest_path* must be an absolute path; any missing parent
        directories are created. Returns the (possibly created) path.

        Raises BlobClientError on HTTP or I/O failure.
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        logger.debug("downloading blob %s → %s", blob_id, dest)
        try:
            async with self._client.stream(
                "GET",
                f"/api/blobs/{blob_id}",
                headers=self._auth,
            ) as resp:
                if resp.status_code == 404:
                    raise BlobClientError(
                        f"blob {blob_id} not found on server"
                    )
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK):
                        fh.write(chunk)
        except BlobClientError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise BlobClientError(
                f"failed to download blob {blob_id}: {exc}"
            ) from exc

        logger.info(
            "blob downloaded: %s → %s (%d bytes)",
            blob_id,
            dest,
            dest.stat().st_size,
        )
        return dest

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload(
        self,
        source_path: Path | str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        purpose: str = "output",
        task_id: str | None = None,
    ) -> str:
        """Upload the file at *source_path* and return the new blob_id.

        *filename* defaults to the basename of *source_path*.
        *content_type* is guessed from the filename if not provided.
        *purpose* should be ``'output'`` for agent-produced files.

        Raises BlobClientError on HTTP or I/O failure.
        """
        src = Path(source_path)
        if not filename:
            filename = src.name
        if not content_type:
            content_type, _ = mimetypes.guess_type(filename)
            content_type = content_type or "application/octet-stream"

        logger.debug("uploading %s as blob (purpose=%s)", src, purpose)
        try:
            with src.open("rb") as fh:
                resp = await self._client.post(
                    "/api/blobs",
                    headers=self._auth,
                    files={"file": (filename, fh, content_type)},
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BlobClientError(
                f"failed to upload {src}: {exc}"
            ) from exc
        except OSError as exc:
            raise BlobClientError(
                f"failed to read {src} for upload: {exc}"
            ) from exc

        blob_id: str = resp.json()["id"]
        logger.info(
            "blob uploaded: %s → id=%s (%d bytes)",
            src,
            blob_id,
            src.stat().st_size,
        )
        return blob_id

    # ------------------------------------------------------------------
    # Convenience helpers for the agent's _run_assignment hooks
    # ------------------------------------------------------------------

    async def download_all(
        self,
        blob_specs: list[dict],
        base_dir: Path,
    ) -> list[Path]:
        """Download a list of blob specs into *base_dir*.

        Each spec must have:
            blob_id (str) — the UUID of the blob to download
            filename (str) — destination filename relative to base_dir

        Returns a list of absolute destination paths in spec order.

        If any download fails, BlobClientError is raised (later
        downloads in the list are not attempted).
        """
        paths: list[Path] = []
        base_resolved = base_dir.resolve()
        for spec in blob_specs:
            blob_id = spec["blob_id"]
            # Never trust the filename as a path: reduce to a bare basename and
            # verify the resolved destination stays inside base_dir. Without
            # this a crafted filename ("../../etc/cron.d/x") writes blob bytes
            # to an arbitrary host path.
            filename = safe_blob_name(spec.get("filename"), blob_id)
            dest = base_dir / filename
            if not dest.resolve().is_relative_to(base_resolved):
                raise BlobClientError(
                    f"refusing unsafe blob destination for {blob_id}: {spec.get('filename')!r}"
                )
            paths.append(await self.download(blob_id, dest))
        return paths

    async def upload_dir(
        self,
        output_dir: Path,
        task_id: str | None = None,
    ) -> list[str]:
        """Upload every file in *output_dir* and return their blob IDs.

        Does not recurse into sub-directories. Files are uploaded in
        sorted order for reproducibility.

        Returns an empty list (without raising) if *output_dir* does
        not exist or is empty — the container may have produced no
        output and the agent handles that gracefully.
        """
        if not output_dir.is_dir():
            return []

        blob_ids: list[str] = []
        for fname in sorted(os.listdir(output_dir)):
            fpath = output_dir / fname
            if not fpath.is_file():
                continue
            try:
                blob_id = await self.upload(
                    fpath,
                    purpose="output",
                    task_id=task_id,
                )
                blob_ids.append(blob_id)
            except BlobClientError as exc:
                # Log and continue — partial output is better than none.
                logger.warning(
                    "skipping output file %s: %s", fpath, exc
                )
        return blob_ids
