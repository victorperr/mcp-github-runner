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


def test_restart_fails_unfinished_calls(tmp_path):
    # A crash used to leave calls pending forever, so the repo hit max_concurrent_calls for good.
    path = str(tmp_path / "s.db")
    store = Store(path)
    for i in range(3):
        store.create_call(f"call-{i}-000000000000000", "1", "add", {"a": i})
    store.claim_call("call-0-000000000000000", "9")
    assert store.count_active_calls("1") == 3

    restarted = Store(path)
    assert restarted.fail_unfinished_calls() == 3
    assert restarted.count_active_calls("1") == 0
    assert restarted.get_call("call-1-000000000000000").arguments is None


def test_purge_keeps_active_and_recent_calls(tmp_path):
    import time

    store = Store(str(tmp_path / "s.db"))
    store.create_call("old-done-00000000000000", "1", "add", {})
    store.finish_call("old-done-00000000000000", "done", from_status=("pending",))
    store.create_call("old-pending-00000000000", "1", "add", {})
    assert store.purge_calls(time.time() + 1) == 1  # only the finished one
    assert store.get_call("old-pending-00000000000") is not None
    assert store.purge_calls(time.time() - 3600) == 0


def test_hourly_count_uses_time_index(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    plan = store._db.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM calls WHERE repository_id = ? AND created_at >= ?", ("1", 0)
    ).fetchall()
    assert any("calls_by_repo_time" in row[-1] for row in plan)


def test_token_check():
    import hashlib

    good = hashlib.sha256(b"t" * 40).hexdigest()
    assert _token_ok("t" * 40, good)
    assert not _token_ok("u" * 40, good)
    assert not _token_ok("t" * 40, None)
    assert not _token_ok("", hashlib.sha256(b"").hexdigest())
