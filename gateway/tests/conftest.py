from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "template"


class RecordingDispatcher:
    def __init__(self, statuses=None):
        self.calls: list[tuple[str, str | None]] = []
        self.statuses = list(statuses or [])  # RunInfo values returned by run_status, in order
        self.verify = True
        self.verify_manifest = False
        self.installed = True

    async def is_installed(self, server):
        if isinstance(self.installed, Exception):
            raise self.installed
        return self.installed

    async def dispatch(self, server, call_id, approval_env=None):
        self.calls.append((call_id, approval_env))

    async def run_status(self, server, call_id):
        return self.statuses.pop(0) if self.statuses else None

    async def verify_run(self, server, call_id, run_id):
        return self.verify

    async def verify_manifest_run(self, server, run_id):
        return self.verify_manifest


def make_settings(tmp_path: Path, **overrides) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "gw.db")
    s.dev_mode = True
    s.dev_key_path = str(tmp_path / "dev-key.pem")
    s.public_url = "http://localhost:8000"  # dev mode only starts on a loopback URL
    s.call_timeout = 5
    s.heartbeat_interval = 0.2
    for k, v in overrides.items():
        setattr(s, k, v)
    if "oidc_audience" not in overrides:
        s.oidc_audience = s.public_url.rstrip("/")  # what Settings derives by default
    return s


@pytest.fixture
def dispatcher():
    return RecordingDispatcher()


@pytest.fixture
def app(tmp_path, dispatcher):
    return create_app(make_settings(tmp_path), dispatcher=dispatcher)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sign(app):
    signer = app.state.dev_signer

    def _sign(event_name="push", repository="me/demo", repository_id="1", **kw):
        return signer.sign(
            audience=app.state.settings.oidc_audience,
            repository=repository,
            repository_id=repository_id,
            event_name=event_name,
            **kw,
        )

    return _sign
