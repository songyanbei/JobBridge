from types import SimpleNamespace

import pytest
import httpx

from app.wecom.identity_client import IdentityClientError, WeComIdentityAppClient


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Transport:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []
        self.gets = 0

    def get(self, *args, **kwargs):
        self.gets += 1
        return _Response({"errcode": 0, "access_token": "secret-token", "expires_in": 7200})

    def post(self, url, **kwargs):
        self.posts.append(kwargs["json"]["open_userid_list"])
        return _Response(self.payload)


def test_directory_visibility_returns_boolean_without_pii():
    class _DirectoryTransport(_Transport):
        def get(self, url, **kwargs):
            self.gets += 1
            if url.endswith("/gettoken"):
                return _Response({"errcode": 0, "access_token": "token", "expires_in": 7200})
            return _Response({"errcode": 0, "userid": "canonical-a", "name": "Sensitive Name", "mobile": "13800000000"})
    transport = _DirectoryTransport({})
    client = WeComIdentityAppClient("corp", "secret", transport=transport)
    visible, reason = client.is_canonical_user_visible("canonical-a")
    assert (visible, reason) == (True, "visible")


def test_directory_not_visible_and_timeout_fail_closed():
    class _NotVisibleTransport(_Transport):
        def get(self, url, **kwargs):
            if url.endswith("/gettoken"):
                return _Response({"errcode": 0, "access_token": "token", "expires_in": 7200})
            return _Response({"errcode": 60111, "errmsg": "not found"})
    client = WeComIdentityAppClient("corp", "secret", transport=_NotVisibleTransport({}))
    assert client.is_canonical_user_visible("canonical-a") == (False, "directory_not_visible")

    class _TimeoutTransport(_Transport):
        def get(self, url, **kwargs):
            if url.endswith("/gettoken"):
                return _Response({"errcode": 0, "access_token": "token", "expires_in": 7200})
            raise httpx.ReadTimeout("timeout")
    timeout_client = WeComIdentityAppClient("corp", "secret", transport=_TimeoutTransport({}))
    assert timeout_client.is_canonical_user_visible("canonical-a") == (False, "directory_unavailable")


def test_batch_maps_by_open_userid_and_caches_token():
    transport = _Transport({
        "errcode": 0,
        "userid_list": [{"open_userid": "open-b", "userid": "canonical-b"}, {"open_userid": "open-a", "userid": "canonical-a"}],
        "invalid_open_userid_list": [],
    })
    client = WeComIdentityAppClient("corp", "identity-secret", transport=transport)
    result = client.batch_openuserid_to_userid(["open-a", "open-b", "open-a"])
    assert result.mapping == {"open-a": "canonical-a", "open-b": "canonical-b"}
    assert transport.gets == 1
    assert transport.posts == [["open-a", "open-b"]]


def test_batch_chunks_at_1000_and_preserves_invalid():
    values = [f"open-{i}" for i in range(1001)]
    class _ChunkTransport(_Transport):
        def post(self, url, **kwargs):
            chunk = kwargs["json"]["open_userid_list"]
            self.posts.append(chunk)
            return _Response({"errcode": 0, "userid_list": [{"open_userid": v, "userid": v.replace("open-", "u-")} for v in chunk if v != values[-1]], "invalid_open_userid_list": [v for v in chunk if v == values[-1]]})
    transport = _ChunkTransport({})
    client = WeComIdentityAppClient("corp", "secret", transport=transport)
    result = client.batch_openuserid_to_userid(values)
    assert result.batches == 2
    assert max(map(len, transport.posts)) <= 1000
    assert values[-1] in result.invalid


def test_incomplete_mapping_is_retryable_error():
    transport = _Transport({"errcode": 0, "userid_list": [], "invalid_open_userid_list": []})
    client = WeComIdentityAppClient("corp", "secret", transport=transport)
    with pytest.raises(IdentityClientError) as exc:
        client.batch_openuserid_to_userid(["open-a"])
    assert exc.value.code == "conversion_incomplete"
    assert exc.value.retryable
