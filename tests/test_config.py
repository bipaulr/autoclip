from autoclip.config import Settings


def test_defaults_without_env_file(monkeypatch):
    for k in ("LLM_PROVIDER", "GEMINI_MODEL", "IG_GRAPH_HOST", "IG_REFRESH_THRESHOLD_DAYS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "gemini"
    assert s.gemini_model == ""  # model names are never hard-coded
    assert s.ig_graph_host == "graph.facebook.com"
    assert s.ig_refresh_threshold_days == 15


def test_values_come_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "some-model")
    monkeypatch.setenv("IG_REFRESH_THRESHOLD_DAYS", "7")
    monkeypatch.setenv("SOMETHING_UNKNOWN", "ignored")
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.ollama_model, s.ig_refresh_threshold_days) == ("ollama", "some-model", 7)


def test_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    f = tmp_path / ".env"
    f.write_text("TELEGRAM_CHAT_ID=12345\n", encoding="utf-8")
    assert Settings(_env_file=f).telegram_chat_id == "12345"
