import pytest

from app.wecom.aibot_client import AibotClient, AibotClientError


def test_official_subscribe_fixture_and_secret_only_in_body():
    client = AibotClient("BOTID", "SECRET")
    frame = client.subscribe("sub-1")
    assert frame == {"cmd": "aibot_subscribe", "headers": {"req_id": "sub-1"}, "body": {"bot_id": "BOTID", "secret": "SECRET"}}


def test_stream_req_id_and_finish_constraints():
    client = AibotClient("BOTID", "SECRET")
    first = client.stream("msg-1", "stream-1", "part")
    assert first["body"]["stream"]["finish"] is False
    with pytest.raises(AibotClientError):
        client.respond_update_msg("other", "stream-1", "bad")
    final = client.respond_update_msg("msg-1", "stream-1", "done", finish=True)
    assert final["cmd"] == "aibot_respond_update_msg"
    with pytest.raises(AibotClientError):
        client.stream("msg-1", "stream-1", "again")

