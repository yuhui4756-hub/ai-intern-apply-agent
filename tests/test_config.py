import os

from app.config import mask_secret, set_env_value


def test_set_env_value_writes_file_and_updates_process_env(tmp_path):
    env_path = tmp_path / ".env"

    set_env_value("TEST_AGENT_API_KEY", "test-secret-value", env_path)

    assert "TEST_AGENT_API_KEY=test-secret-value" in env_path.read_text(encoding="utf-8")
    assert os.environ["TEST_AGENT_API_KEY"] == "test-secret-value"
    assert mask_secret(os.environ["TEST_AGENT_API_KEY"]) == "tes********alue"
