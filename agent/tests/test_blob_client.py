"""Unit tests for agent.blob_client.

All HTTP is intercepted with httpx.MockTransport — no real network or
HADCD backend required. All file I/O uses pytest's tmp_path.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from agent.blob_client import BlobClient, BlobClientError


# -------------------------------------------------------------------------
# Helper
# -------------------------------------------------------------------------


def _make_client(
    handler,
    tmp_path: Path,
    *,
    node_token: str = "tok-test",
) -> BlobClient:
    """Return a BlobClient whose HTTP requests are served by *handler*."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://backend.test",
    )
    return BlobClient(
        client=http,
        node_token=node_token,
        blob_storage_dir=str(tmp_path),
    )


# -------------------------------------------------------------------------
# download()
# -------------------------------------------------------------------------


async def test_download_writes_bytes_to_dest(tmp_path):
    blob_id = str(uuid.uuid4())
    content = b"hello blob"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    bc = _make_client(handler, tmp_path)
    dest = tmp_path / "out.bin"
    result = await bc.download(blob_id, dest)

    assert result == dest
    assert dest.read_bytes() == content


async def test_download_creates_missing_parent_directories(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data")

    bc = _make_client(handler, tmp_path)
    deep = tmp_path / "a" / "b" / "c.bin"
    await bc.download(str(uuid.uuid4()), deep)
    assert deep.read_bytes() == b"data"


async def test_download_raises_blob_client_error_on_404(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    bc = _make_client(handler, tmp_path)
    with pytest.raises(BlobClientError, match="not found"):
        await bc.download(str(uuid.uuid4()), tmp_path / "out.bin")


async def test_download_raises_blob_client_error_on_server_error(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    bc = _make_client(handler, tmp_path)
    with pytest.raises(BlobClientError):
        await bc.download(str(uuid.uuid4()), tmp_path / "out.bin")


async def test_download_sends_bearer_auth_header(tmp_path):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, content=b"ok")

    bc = _make_client(handler, tmp_path, node_token="my-node-token")
    await bc.download(str(uuid.uuid4()), tmp_path / "f.bin")
    assert seen["auth"] == "Bearer my-node-token"


async def test_download_hits_blobs_endpoint(tmp_path):
    bid = str(uuid.uuid4())
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, content=b"ok")

    bc = _make_client(handler, tmp_path)
    await bc.download(bid, tmp_path / "f.bin")
    assert seen["path"] == f"/api/blobs/{bid}"


async def test_download_large_payload_round_trips(tmp_path):
    """Ensure multi-chunk streaming reassembles correctly."""
    # Content larger than the default _DOWNLOAD_CHUNK (65536)
    content = b"z" * (65536 * 2 + 1)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    bc = _make_client(handler, tmp_path)
    dest = tmp_path / "large.bin"
    await bc.download(str(uuid.uuid4()), dest)
    assert dest.read_bytes() == content


# -------------------------------------------------------------------------
# upload()
# -------------------------------------------------------------------------


async def test_upload_returns_blob_id_from_response(tmp_path):
    src = tmp_path / "input.txt"
    src.write_bytes(b"model weights")
    expected_id = str(uuid.uuid4())

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": expected_id})

    bc = _make_client(handler, tmp_path)
    result = await bc.upload(src)
    assert result == expected_id


async def test_upload_posts_to_blobs_endpoint(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"id": "abc"})

    bc = _make_client(handler, tmp_path)
    await bc.upload(src)
    assert seen["path"] == "/api/blobs"


async def test_upload_sends_bearer_auth_header(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"id": "abc"})

    bc = _make_client(handler, tmp_path, node_token="node-tok")
    await bc.upload(src)
    assert seen["auth"] == "Bearer node-tok"


async def test_upload_raises_blob_client_error_on_http_error(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="storage full")

    bc = _make_client(handler, tmp_path)
    with pytest.raises(BlobClientError):
        await bc.upload(src)


async def test_upload_uses_src_basename_as_filename_by_default(tmp_path):
    src = tmp_path / "my_model.onnx"
    src.write_bytes(b"onnx")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content
        return httpx.Response(200, json={"id": "x"})

    bc = _make_client(handler, tmp_path)
    await bc.upload(src)
    # The multipart body should contain the original filename
    assert b"my_model.onnx" in seen["body"]


# -------------------------------------------------------------------------
# download_all()
# -------------------------------------------------------------------------


async def test_download_all_fetches_each_blob_in_order(tmp_path):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    def handler(req: httpx.Request) -> httpx.Response:
        for i, bid in enumerate(ids):
            if bid in str(req.url):
                return httpx.Response(200, content=f"content-{i}".encode())
        return httpx.Response(404)

    bc = _make_client(handler, tmp_path)
    dest_dir = tmp_path / "dl"
    dest_dir.mkdir()
    specs = [
        {"blob_id": ids[0], "filename": "a.bin"},
        {"blob_id": ids[1], "filename": "b.bin"},
    ]
    paths = await bc.download_all(specs, dest_dir)

    assert len(paths) == 2
    assert paths[0].read_bytes() == b"content-0"
    assert paths[1].read_bytes() == b"content-1"


async def test_download_all_uses_blob_id_as_filename_when_absent(tmp_path):
    bid = str(uuid.uuid4())

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data")

    bc = _make_client(handler, tmp_path)
    dest_dir = tmp_path / "dl"
    dest_dir.mkdir()
    paths = await bc.download_all([{"blob_id": bid}], dest_dir)
    assert paths[0].name == bid


async def test_download_all_stops_on_first_failure(tmp_path):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if ids[0] in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"second")

    bc = _make_client(handler, tmp_path)
    dest_dir = tmp_path / "dl"
    dest_dir.mkdir()
    specs = [
        {"blob_id": ids[0], "filename": "a.bin"},
        {"blob_id": ids[1], "filename": "b.bin"},
    ]
    with pytest.raises(BlobClientError):
        await bc.download_all(specs, dest_dir)
    # Second blob should never have been attempted
    assert call_count == 1


# -------------------------------------------------------------------------
# upload_dir()
# -------------------------------------------------------------------------


async def test_upload_dir_uploads_every_file(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "a.txt").write_bytes(b"aaa")
    (output_dir / "b.txt").write_bytes(b"bbb")

    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"id": f"blob-{call_count}"})

    bc = _make_client(handler, tmp_path)
    ids = await bc.upload_dir(output_dir)
    assert ids == ["blob-1", "blob-2"]
    assert call_count == 2


async def test_upload_dir_uploads_in_sorted_order(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    # Write in reverse sorted order to confirm sort is applied
    for name in ["z.txt", "a.txt", "m.txt"]:
        (output_dir / name).write_bytes(b"x")

    seen_names: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        # httpx encodes multipart; the filename appears in the body
        body = req.content
        for name in ["a.txt", "m.txt", "z.txt"]:
            if name.encode() in body:
                seen_names.append(name)
        return httpx.Response(200, json={"id": "x"})

    bc = _make_client(handler, tmp_path)
    await bc.upload_dir(output_dir)
    assert seen_names == ["a.txt", "m.txt", "z.txt"]


async def test_upload_dir_returns_empty_list_for_missing_dir(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    bc = _make_client(handler, tmp_path)
    ids = await bc.upload_dir(tmp_path / "does_not_exist")
    assert ids == []


async def test_upload_dir_returns_empty_list_for_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"id": "x"})

    bc = _make_client(handler, tmp_path)
    ids = await bc.upload_dir(empty)
    assert ids == []
    assert call_count == 0


async def test_upload_dir_skips_failed_file_and_continues(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "a.txt").write_bytes(b"aaa")
    (output_dir / "b.txt").write_bytes(b"bbb")

    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500, text="fail")
        return httpx.Response(200, json={"id": "blob-2"})

    bc = _make_client(handler, tmp_path)
    ids = await bc.upload_dir(output_dir)
    assert ids == ["blob-2"]
    assert call_count == 2


async def test_upload_dir_ignores_subdirectories(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "file.txt").write_bytes(b"data")
    (output_dir / "subdir").mkdir()

    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"id": f"b{call_count}"})

    bc = _make_client(handler, tmp_path)
    ids = await bc.upload_dir(output_dir)
    assert ids == ["b1"]
    assert call_count == 1


# -------------------------------------------------------------------------
# safe_blob_name / path-traversal defense (security regression)
# -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("data.csv", "data.csv"),
        ("../../etc/cron.d/pwn", "pwn"),
        ("../../../../etc/passwd", "passwd"),
        ("/etc/shadow", "shadow"),
        (r"..\..\Windows\System32\x", "x"),  # Windows-style separators
        ("subdir/leaf.bin", "leaf.bin"),
        ("..", "FALLBACK"),
        (".", "FALLBACK"),
        ("", "FALLBACK"),
        (None, "FALLBACK"),
    ],
)
def test_safe_blob_name_reduces_to_basename(raw, expected):
    from agent.blob_client import safe_blob_name

    assert safe_blob_name(raw, "FALLBACK") == expected


async def test_download_all_neutralizes_traversal_filename(tmp_path):
    """A crafted filename must land inside base_dir, not at a traversed path.

    Regression: a client-controlled blob filename of '../../etc/cron.d/x' was
    written to an arbitrary host path (arbitrary-write → RCE on the node).
    """
    base_dir = tmp_path / "input"
    base_dir.mkdir()
    escaped_target = tmp_path / "cron.d"  # would-be traversal destination

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"malicious")

    bc = _make_client(handler, tmp_path)
    spec = {"blob_id": str(uuid.uuid4()), "filename": "../cron.d/pwn"}
    (path,) = await bc.download_all([spec], base_dir)

    # Written safely inside base_dir under a basename, never at the escape path.
    assert path.parent.resolve() == base_dir.resolve()
    assert path.name == "pwn"
    assert not (escaped_target / "pwn").exists()
    assert not escaped_target.exists()
