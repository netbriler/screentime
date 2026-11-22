import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep the developer's real config and state out of tests."""
    monkeypatch.setenv("SCREENTIME_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("HOME", str(tmp_path))
