from autoclip.token_store import EnvFileTokenStore, MemoryTokenStore


def test_env_file_get_and_update_preserves_other_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nA=1\nIG_ACCESS_TOKEN=old\nB=x=y\n", encoding="utf-8")
    store = EnvFileTokenStore(f)
    assert store.get("IG_ACCESS_TOKEN") == "old"
    assert store.get("B") == "x=y"
    assert store.get("MISSING") is None

    store.set(IG_ACCESS_TOKEN="new", C="3")
    assert f.read_text(encoding="utf-8") == "# comment\nA=1\nIG_ACCESS_TOKEN=new\nB=x=y\nC=3\n"
    assert store.get("IG_ACCESS_TOKEN") == "new"


def test_env_file_missing_file(tmp_path):
    store = EnvFileTokenStore(tmp_path / "nope.env")
    assert store.get("A") is None
    store.set(A="1")
    assert store.get("A") == "1"


def test_empty_value_reads_as_none(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=\n", encoding="utf-8")
    assert EnvFileTokenStore(f).get("A") is None


def test_memory_store():
    store = MemoryTokenStore(A="1")
    store.set(A="2", B="3")
    assert (store.get("A"), store.get("B"), store.get("C")) == ("2", "3", None)
