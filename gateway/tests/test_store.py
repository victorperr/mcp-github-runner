"""Store: server cache, auth lookup."""

from app.mcp_endpoint import _token_ok
from app.store import Store

H1, H2 = "1" * 64, "2" * 64


def _upsert(store, manifest, token_hash=H1, name="me/demo", repo_id="1"):
    store.upsert_server(repository_id=repo_id, full_name=name, workflow_path=".github/workflows/mcp.yml",
                        ref="refs/heads/main", manifest=manifest, token_hash=token_hash)


def test_server_is_cached_until_upsert(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    _upsert(store, {"tools": [{"name": "a"}]})
    first = store.get_server("1")
    assert store.get_server("1") is first  # cached: no re-parse
    _upsert(store, {"tools": [{"name": "b"}]})
    fresh = store.get_server("1")
    assert fresh is not first and fresh.manifest["tools"][0]["name"] == "b"


def test_read_racing_an_upsert_is_not_cached(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    _upsert(store, {"tools": []})
    real_server = store._server

    def parse_then_upsert(row):
        record = real_server(row)
        _upsert(store, {"tools": [{"name": "new"}]})  # write lands while the old row is parsed
        return record

    store._server = parse_then_upsert
    store.get_server("1")
    store._server = real_server
    assert store.get_server("1").manifest["tools"] == [{"name": "new"}]


def test_find_token_hash_and_rename(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    _upsert(store, {}, H1)
    assert store.find_token_hash("ME/DEMO") == ("1", H1)  # names are case-insensitive
    _upsert(store, {}, None, name="me/renamed")  # rename keeps the id and the token
    assert store.find_token_hash("me/demo") is None
    assert store.find_token_hash("me/renamed") == ("1", H1)
    assert store.get_server_by_name("me/renamed").repository_id == "1"


def test_token_check():
    import hashlib

    good = hashlib.sha256(b"t" * 40).hexdigest()
    assert _token_ok("t" * 40, good)
    assert not _token_ok("u" * 40, good)
    assert not _token_ok("t" * 40, None)
    assert not _token_ok("", hashlib.sha256(b"").hexdigest())
