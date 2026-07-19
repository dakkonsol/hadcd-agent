"""Unit tests for agent.image_cache (Phase 10d).

All Docker SDK calls are intercepted via unittest.mock — no real
Docker daemon required. Tests cover:
  * prepull_all: empty list, already cached, successful pull, individual
    pull failure, no docker SDK, daemon unreachable.
  * maybe_gc: budget disabled (0), under budget, dangling prune only,
    dangling + unused prune, no docker SDK, daemon unreachable,
    usage measurement failure.
  * build_image_cache: comma-separated parsing, empty strings stripped.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch


from agent.image_cache import ImageCacheManager, build_image_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIB = 1024 ** 3


def _make_image(tags: list[str], size_bytes: int = 0) -> MagicMock:
    """Return a mock Docker image object."""
    img = MagicMock()
    img.tags = tags
    img.attrs = {"Size": size_bytes}
    return img


def _mock_docker_module(client: MagicMock) -> ModuleType:
    """Return a mock `docker` module whose from_env() returns *client*."""
    mod = MagicMock(name="docker")
    mod.from_env.return_value = client
    mod.errors = MagicMock()
    mod.errors.DockerException = Exception
    return mod


# ---------------------------------------------------------------------------
# prepull_all — no images configured
# ---------------------------------------------------------------------------


def test_prepull_all_noop_when_no_images():
    """With an empty prepull list, Docker is never even imported."""
    mgr = ImageCacheManager(prepull_images=[], budget_gb=50.0)
    # Should complete silently; no import / no client call needed.
    mgr.prepull_all()  # must not raise


# ---------------------------------------------------------------------------
# prepull_all — docker SDK absent
# ---------------------------------------------------------------------------


def test_prepull_all_warns_when_no_docker_sdk(caplog):
    mgr = ImageCacheManager(prepull_images=["ollama/ollama:latest"], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": None}):
        with caplog.at_level("WARNING", logger="hadcd.agent.image_cache"):
            mgr.prepull_all()
    assert "docker SDK not installed" in caplog.text


# ---------------------------------------------------------------------------
# prepull_all — daemon unreachable
# ---------------------------------------------------------------------------


def test_prepull_all_warns_when_daemon_unreachable(caplog):
    mgr = ImageCacheManager(prepull_images=["ollama/ollama:latest"], budget_gb=50.0)
    mock_docker = _mock_docker_module(MagicMock())
    mock_docker.from_env.side_effect = Exception("daemon not running")
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        with caplog.at_level("WARNING", logger="hadcd.agent.image_cache"):
            mgr.prepull_all()
    assert "cannot connect to Docker daemon" in caplog.text


# ---------------------------------------------------------------------------
# prepull_all — image already present (skip)
# ---------------------------------------------------------------------------


def test_prepull_all_skips_already_cached_image():
    client = MagicMock()
    client.images.list.return_value = [
        _make_image(["ollama/ollama:latest"]),
    ]
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=["ollama/ollama:latest"], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.prepull_all()

    client.images.pull.assert_not_called()


# ---------------------------------------------------------------------------
# prepull_all — missing image pulled successfully
# ---------------------------------------------------------------------------


def test_prepull_all_pulls_missing_image():
    client = MagicMock()
    client.images.list.return_value = []  # nothing cached
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=["ollama/ollama:latest"], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.prepull_all()

    client.images.pull.assert_called_once_with("ollama/ollama:latest")


# ---------------------------------------------------------------------------
# prepull_all — partial failure: one image fails, others continue
# ---------------------------------------------------------------------------


def test_prepull_all_continues_after_individual_pull_failure(caplog):
    client = MagicMock()
    client.images.list.return_value = []

    pulls = []

    def _pull(image_ref: str):
        pulls.append(image_ref)
        if image_ref == "bad/image:latest":
            raise RuntimeError("pull failed")

    client.images.pull.side_effect = _pull
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(
        prepull_images=["bad/image:latest", "good/image:latest"],
        budget_gb=50.0,
    )
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        with caplog.at_level("WARNING", logger="hadcd.agent.image_cache"):
            mgr.prepull_all()

    # Both attempted; warning logged for the bad one.
    assert "bad/image:latest" in pulls
    assert "good/image:latest" in pulls
    assert "failed to pull" in caplog.text


# ---------------------------------------------------------------------------
# prepull_all — mixed present + missing
# ---------------------------------------------------------------------------


def test_prepull_all_skips_present_pulls_missing():
    client = MagicMock()
    client.images.list.return_value = [_make_image(["cached/image:latest"])]
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(
        prepull_images=["cached/image:latest", "missing/image:latest"],
        budget_gb=50.0,
    )
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.prepull_all()

    # Only the missing image should be pulled.
    client.images.pull.assert_called_once_with("missing/image:latest")


# ---------------------------------------------------------------------------
# maybe_gc — budget disabled
# ---------------------------------------------------------------------------


def test_maybe_gc_noop_when_budget_zero():
    """budget_gb=0 must not connect to Docker at all."""
    mgr = ImageCacheManager(prepull_images=[], budget_gb=0)
    # If Docker is imported and called this would raise — we want silence.
    with patch.dict(sys.modules, {"docker": None}):
        mgr.maybe_gc()  # must not raise


# ---------------------------------------------------------------------------
# maybe_gc — no docker SDK
# ---------------------------------------------------------------------------


def test_maybe_gc_silent_when_no_docker_sdk():
    mgr = ImageCacheManager(prepull_images=[], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": None}):
        mgr.maybe_gc()  # must not raise


# ---------------------------------------------------------------------------
# maybe_gc — daemon unreachable
# ---------------------------------------------------------------------------


def test_maybe_gc_warns_when_daemon_unreachable(caplog):
    mock_docker = _mock_docker_module(MagicMock())
    mock_docker.from_env.side_effect = Exception("daemon not running")

    mgr = ImageCacheManager(prepull_images=[], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        with caplog.at_level("WARNING", logger="hadcd.agent.image_cache"):
            mgr.maybe_gc()

    assert "cannot connect to Docker daemon" in caplog.text


# ---------------------------------------------------------------------------
# maybe_gc — under budget: no prune
# ---------------------------------------------------------------------------


def test_maybe_gc_no_prune_when_under_budget():
    budget_gb = 50.0
    # Usage is 10 GiB — well under budget.
    usage_bytes = int(10 * _GIB)

    client = MagicMock()
    client.df.return_value = {
        "Images": [{"Size": usage_bytes}],
    }
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=[], budget_gb=budget_gb)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.maybe_gc()

    client.images.prune.assert_not_called()


# ---------------------------------------------------------------------------
# maybe_gc — over budget, dangling prune brings it under
# ---------------------------------------------------------------------------


def test_maybe_gc_prunes_dangling_when_over_budget():
    budget_gb = 10.0
    over_budget = int(20 * _GIB)
    after_dangling = int(5 * _GIB)  # under budget after dangling prune

    call_count = [0]

    def _df():
        call_count[0] += 1
        size = over_budget if call_count[0] == 1 else after_dangling
        return {"Images": [{"Size": size}]}

    client = MagicMock()
    client.df.side_effect = _df
    client.images.prune.return_value = {"SpaceReclaimed": over_budget - after_dangling}
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=[], budget_gb=budget_gb)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.maybe_gc()

    # Only dangling prune should be called (budget satisfied afterwards).
    client.images.prune.assert_called_once_with(filters={"dangling": True})


# ---------------------------------------------------------------------------
# maybe_gc — dangling not enough, unused prune also runs
# ---------------------------------------------------------------------------


def test_maybe_gc_prunes_unused_when_still_over_budget_after_dangling():
    budget_gb = 10.0
    over_budget = int(20 * _GIB)
    after_dangling = int(15 * _GIB)   # still over budget

    call_count = [0]

    def _df():
        call_count[0] += 1
        if call_count[0] == 1:
            return {"Images": [{"Size": over_budget}]}
        return {"Images": [{"Size": after_dangling}]}

    client = MagicMock()
    client.df.side_effect = _df
    client.images.prune.return_value = {"SpaceReclaimed": 0}
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=[], budget_gb=budget_gb)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        mgr.maybe_gc()

    # Both dangling AND unused prune must be called.
    calls = [c.kwargs for c in client.images.prune.call_args_list]
    assert {"filters": {"dangling": True}} in calls
    assert {"filters": {"dangling": False}} in calls


# ---------------------------------------------------------------------------
# maybe_gc — usage measurement failure is handled gracefully
# ---------------------------------------------------------------------------


def test_maybe_gc_handles_df_failure_gracefully(caplog):
    client = MagicMock()
    client.df.side_effect = RuntimeError("df exploded")
    # Fallback: images.list also fails.
    client.images.list.side_effect = RuntimeError("list exploded")
    mock_docker = _mock_docker_module(client)

    mgr = ImageCacheManager(prepull_images=[], budget_gb=50.0)
    with patch.dict(sys.modules, {"docker": mock_docker, "docker.errors": mock_docker.errors}):
        with caplog.at_level("WARNING", logger="hadcd.agent.image_cache"):
            mgr.maybe_gc()

    # Agent should log a warning and continue without crashing.
    assert "could not measure image disk usage" in caplog.text
    client.images.prune.assert_not_called()


# ---------------------------------------------------------------------------
# build_image_cache — parses comma-separated list correctly
# ---------------------------------------------------------------------------


def test_build_image_cache_parses_comma_list():
    settings = MagicMock()
    settings.docker_prepull_images = "img1:latest, img2:v2 , , img3:stable"
    settings.docker_image_budget_gb = 30.0

    mgr = build_image_cache(settings)

    assert mgr._prepull == ["img1:latest", "img2:v2", "img3:stable"]
    assert mgr._budget_bytes == int(30.0 * _GIB)


def test_build_image_cache_empty_string_gives_no_prepull():
    settings = MagicMock()
    settings.docker_prepull_images = ""
    settings.docker_image_budget_gb = 50.0

    mgr = build_image_cache(settings)
    assert mgr._prepull == []


def test_build_image_cache_zero_budget_disables_gc():
    settings = MagicMock()
    settings.docker_prepull_images = ""
    settings.docker_image_budget_gb = 0.0

    mgr = build_image_cache(settings)
    assert mgr._budget_bytes == 0
