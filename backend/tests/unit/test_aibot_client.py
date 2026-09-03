import pytest

from app.wecom.aibot_client import AibotClient, AibotClientError, stable_aibot_stream_id


def test_official_subscribe_fixture_and_secret_only_in_body():
    client = AibotClient("BOTID", "SECRET")
    frame = client.subscribe("sub-1")
    assert frame == {"cmd": "aibot_subscribe", "headers": {"req_id": "sub-1"}, "body": {"bot_id": "BOTID", "secret": "SECRET"}}


def test_stream_req_id_and_finish_constraints():
    client = AibotClient("BOTID", "SECRET")
    first = client.stream("msg-1", "stream-1", "part")
    assert first["body"]["stream"]["finish"] is False
    final = client.stream("msg-1", "stream-1", "done", finish=True)
    assert final["cmd"] == "aibot_respond_msg"
    with pytest.raises(AibotClientError):
        client.stream("msg-1", "stream-1", "again")


def test_ordinary_message_reply_is_one_shot_final_stream():
    client = AibotClient("BOTID", "SECRET")
    frame = client.respond_msg("msg-1", "收到")
    assert frame == {
        "cmd": "aibot_respond_msg",
        "headers": {"req_id": "msg-1"},
        "body": {
            "msgtype": "stream",
            "stream": {
                "id": stable_aibot_stream_id("msg-1", 0),
                "finish": True,
                "content": "收到",
            },
        },
    }


def test_stable_stream_id_distinguishes_reply_indexes_and_retries():
    assert stable_aibot_stream_id(7, 0) == stable_aibot_stream_id(7, 0)
    assert stable_aibot_stream_id(7, 0) != stable_aibot_stream_id(7, 1)


def test_template_card_update_uses_official_response_shape():
    client = AibotClient("BOTID", "SECRET")
    frame = client.respond_update_msg("evt-1", {"card_type": "button_interaction"})
    assert frame == {
        "cmd": "aibot_respond_update_msg",
        "headers": {"req_id": "evt-1"},
        "body": {
            "response_type": "update_template_card",
            "template_card": {"card_type": "button_interaction"},
        },
    }


def test_stream_rejects_empty_content_before_provider_write():
    with pytest.raises(AibotClientError, match="stream.content"):
        AibotClient("BOTID", "SECRET").stream("msg-1", "stream-1", "")
