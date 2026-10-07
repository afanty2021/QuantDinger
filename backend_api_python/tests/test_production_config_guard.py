from scripts.check_production_config import validate


def _production_values(**overrides):
    values = {
        "SECRET_KEY": "s" * 32,
        "CREDENTIAL_ENCRYPTION_KEY": "c" * 32,
        "ADMIN_PASSWORD": "unique-admin-password",
        "POSTGRES_PASSWORD": "unique-postgres-password",
        "GRAFANA_ADMIN_PASSWORD": "unique-grafana-password",
    }
    values.update(overrides)
    return values


def test_production_config_requires_32_byte_session_secret():
    errors = validate(_production_values(SECRET_KEY="legacy-secret"))

    assert "SECRET_KEY must contain at least 32 bytes" in errors


def test_production_config_accepts_strong_unique_credentials():
    assert validate(_production_values()) == []


def test_production_config_does_not_require_optional_grafana():
    assert validate(_production_values(GRAFANA_ADMIN_PASSWORD="")) == []


def test_observability_config_requires_strong_grafana_password():
    errors = validate(
        _production_values(GRAFANA_ADMIN_PASSWORD=""),
        require_grafana=True,
    )

    assert (
        "GRAFANA_ADMIN_PASSWORD is missing or uses a known unsafe default" in errors
    )
