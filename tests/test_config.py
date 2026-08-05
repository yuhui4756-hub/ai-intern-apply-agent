import os

from app.config import mask_secret, set_env_value, suggest_api_key_env


def test_set_env_value_writes_file_and_updates_process_env(tmp_path):
    env_path = tmp_path / ".env"

    set_env_value("TEST_AGENT_API_KEY", "test-secret-value", env_path)

    assert "TEST_AGENT_API_KEY=test-secret-value" in env_path.read_text(encoding="utf-8")
    assert os.environ["TEST_AGENT_API_KEY"] == "test-secret-value"
    assert mask_secret(os.environ["TEST_AGENT_API_KEY"]) == "tes********alue"


def test_suggest_api_key_env_from_provider_or_name():
    assert suggest_api_key_env("DeepSeek", "https://api.deepseek.com/v1") == "DEEPSEEK_API_KEY"
    assert suggest_api_key_env("OpenAI", "https://api.openai.com/v1") == "OPENAI_API_KEY"
    assert suggest_api_key_env("Local proxy", "http://127.0.0.1:17200/v1") == "LOCAL_PROXY_API_KEY"
    assert suggest_api_key_env("cheap model", "") == "CHEAP_MODEL_API_KEY"
