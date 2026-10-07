from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from app.services.settings.env_file import ENV_FILE_PATH, read_env_file, write_env_file


def test_default_settings_file_is_the_repository_root_env() -> None:
    assert Path(ENV_FILE_PATH) == Path(__file__).resolve().parents[2] / ".env"


def test_settings_env_file_preserves_special_character_values(tmp_path: Path) -> None:
    env_file = tmp_path / "backend.env"
    password = 'Abc #12"\\path$!=O\'Brien'
    env_file.write_text(
        '# Existing comment\nADMIN_PASSWORD="Abc #12\\"\\\\path$!=O\'Brien"\n',
        encoding="utf-8",
    )

    values = read_env_file(str(env_file))

    assert values["ADMIN_PASSWORD"] == password
    values["OPENAI_MODEL"] = "example/model"
    assert write_env_file(values, str(env_file)) is True
    assert dotenv_values(env_file)["ADMIN_PASSWORD"] == password
    assert dotenv_values(env_file)["OPENAI_MODEL"] == "example/model"
    assert "# Existing comment" in env_file.read_text(encoding="utf-8")
