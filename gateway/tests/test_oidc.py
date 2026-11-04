"""GitHub OIDC verification with a rate-limited JWKS cache."""

import time

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.oidc import GITHUB_ISSUER, GITHUB_JWKS_URL, JWKSCache, OIDCError, OIDCVerifier

AUD = "https://gw.test"


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key, kid="k1"):
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def token(key, kid="k1", **over):
    now = int(time.time())
    claims = {"iss": GITHUB_ISSUER, "aud": AUD, "iat": now, "exp": now + 300,
              "repository": "me/demo", "repository_id": 1, "event_name": "push", "ref": "refs/heads/main",
              "workflow_ref": "me/demo/.github/workflows/mcp.yml@refs/heads/main", "run_id": 5, **over}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def test_verifies_github_token(key):
    with respx.mock:
        respx.get(GITHUB_JWKS_URL).respond(json=jwks(key))
        ident = OIDCVerifier(AUD).verify(token(key))
    assert ident.repository == "me/demo" and ident.workflow_path == ".github/workflows/mcp.yml"


def test_unknown_kids_do_not_refetch_every_time(key):
    verifier = OIDCVerifier(AUD, jwks=JWKSCache(GITHUB_JWKS_URL, min_refresh=60))
    with respx.mock:
        route = respx.get(GITHUB_JWKS_URL).respond(json=jwks(key))
        verifier.verify(token(key))
        for i in range(20):
            with pytest.raises(OIDCError, match="unknown signing key"):
                verifier.verify(token(key, kid=f"garbage-{i}"))
        assert route.call_count == 1


def test_rotated_key_is_picked_up_after_min_refresh(key):
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cache = JWKSCache(GITHUB_JWKS_URL, min_refresh=0)
    verifier = OIDCVerifier(AUD, jwks=cache)
    with respx.mock:
        respx.get(GITHUB_JWKS_URL).respond(json=jwks(key))
        verifier.verify(token(key))
        respx.get(GITHUB_JWKS_URL).respond(json=jwks(new_key, kid="k2"))
        assert verifier.verify(token(new_key, kid="k2")).run_id == "5"


def test_fetch_failure_keeps_known_keys(key):
    import httpx

    cache = JWKSCache(GITHUB_JWKS_URL, min_refresh=0, max_age=0)
    verifier = OIDCVerifier(AUD, jwks=cache)
    with respx.mock:
        respx.get(GITHUB_JWKS_URL).respond(json=jwks(key))
        verifier.verify(token(key))
        respx.get(GITHUB_JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
        assert verifier.verify(token(key)).repository == "me/demo"


def test_token_without_kid_or_garbage_is_rejected(key):
    verifier = OIDCVerifier(AUD, jwks=JWKSCache(GITHUB_JWKS_URL))
    with pytest.raises(OIDCError):
        verifier.verify("not-a-jwt")
    no_kid = jwt.encode({"iss": GITHUB_ISSUER}, key, algorithm="RS256")
    with pytest.raises(OIDCError, match="no key ID"):
        verifier.verify(no_kid)
