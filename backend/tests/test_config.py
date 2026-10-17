"""Configuration loading tests."""

from pathlib import Path

from backend.app.config import ENV_FILE, PROJECT_ROOT, settings


def test_env_file_is_anchored_to_the_project_root():
    # ``run_eval.py`` runs with ``backend/`` as the working directory, so a
    # relative ".env" would silently fall back to the Ollama defaults and the
    # diagnosis panel would report "模型不可用".
    assert ENV_FILE == PROJECT_ROOT / ".env"
    assert Path(settings.model_config["env_file"]) == ENV_FILE
    assert Path(settings.model_config["env_file"]).is_absolute()


def test_project_root_contains_the_backend_package():
    assert (PROJECT_ROOT / "backend" / "app" / "config.py").is_file()
