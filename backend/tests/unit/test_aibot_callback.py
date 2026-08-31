import hashlib
import pytest

from app.wecom.aibot_callback import AibotProtocolError, parse_callback


FIXTURE = {
    "cmd": "aibot_msg_callback",
    "headers": {"req_id": "msg-1"},
    "body": {"msgid": "MSGID", "aibotid": "AIBOTID", "chatid": "CHATID", "chattype": "group", "from": {"userid": "USERID"}, "msgtype": "text", "text": {"content": "@RobotA hello"}},
}


def test_official_group_fixture_projects_ordering_and_dedupe():
    callback = parse_callback(FIXTURE)
    assert callback.provider_msg_id == "MSGID"
    assert callback.conversation_id == "CHATID"
    assert callback.ordering_key == "wecom:wecom_aibot:group:CHATID"
    assert callback.to_wecom_message().schema_version == 2
    assert callback.dedupe_key == hashlib.sha256(b"wecom_aibot\0MSGID").hexdigest()


def test_missing_or_unsupported_fields_fail_closed():
    with pytest.raises(AibotProtocolError):
        parse_callback({**FIXTURE, "cmd": "unknown"})
    with pytest.raises(AibotProtocolError):
        parse_callback({**FIXTURE, "body": {**FIXTURE["body"], "chattype": "group", "chatid": ""}})

