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


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, (False, "directory_invalid")),
        ({"errcode": "0"}, (False, "directory_invalid")),
        ({"errcode": True}, (False, "directory_invalid")),
        ({"errcode": 1.0}, (False, "directory_invalid")),
        ({"errcode": -1}, (False, "unknown_errcode")),
        ({"errcode": 999999}, (False, "unknown_errcode")),
        ({"errcode": 500}, (False, "directory_unavailable")),
        ({"errcode": 50002}, (False, "directory_unavailable")),
        ({"errcode": 0, "userid": "canonical-a"}, (True, "visible")),
    ],
)
def test_directory_errcode_response_matrix(payload, expected):
    class _MatrixTransport(_Transport):
        def get(self, url, **kwargs):
            self.gets += 1
            if url.endswith("/gettoken"):
                return _Response({"errcode": 0, "access_token": "token", "expires_in": 7200})
            return _Response(payload)

    client = WeComIdentityAppClient("corp", "secret", transport=_MatrixTransport({}))
    assert client.is_canonical_user_visible("canonical-a") == expected


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


@pytest.mark.parametrize(
    ("value", "expected_code", "retryable"),
    [
        (None, "token_invalid", False),
        ("0", "token_invalid", False),
        (-1, "unknown_errcode", False),
        (999999, "unknown_errcode", False),
        (42001, "token_err_42001", True),
        (500, "token_err_500", True),
        (50002, "token_err_50002", True),
        (40001, "token_err_40001", False),
    ],
)
def test_token_errcode_classification(value, expected_code, retryable):
    class _TokenTransport(_Transport):
        def get(self, url, **kwargs):
            return _Response({"access_token": "token"} if value is None else {"errcode": value, "access_token": "token"})
    client = WeComIdentityAppClient("corp", "secret", transport=_TokenTransport({}))
    with pytest.raises(IdentityClientError) as exc:
        client.get_access_token()
    assert exc.value.code == expected_code
    assert exc.value.retryable is retryable


@pytest.mark.parametrize(
    ("value", "expected_code", "retryable"),
    [
        (None, "conversion_invalid", False),
        ("0", "conversion_invalid", False),
        (-1, "unknown_errcode", False),
        (999999, "unknown_errcode", False),
        (45009, "conversion_err_45009", True),
        (500, "conversion_err_500", True),
        (50002, "conversion_err_50002", True),
        (40013, "conversion_err_40013", False),
    ],
)
def test_conversion_errcode_classification(value, expected_code, retryable):
    class _ConversionTransport(_Transport):
        def get(self, url, **kwargs):
            return _Response({"errcode": 0, "access_token": "token", "expires_in": 7200})

        def post(self, url, **kwargs):
            payload = {} if value is None else {"errcode": value}
            return _Response(payload)
    client = WeComIdentityAppClient("corp", "secret", transport=_ConversionTransport({}))
    with pytest.raises(IdentityClientError) as exc:
        client.batch_openuserid_to_userid(["open-a"])
    assert exc.value.code == expected_code
    assert exc.value.retryable is retryable
