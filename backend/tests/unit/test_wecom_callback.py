"""企微回调 XML 解析和统一消息对象测试。"""
import pytest

from app.wecom.callback import parse_xml, parse_message, extract_encrypt_from_xml, WeComMessage


class TestParseXml:

    def test_basic_xml(self):
        xml = "<xml><Name>test</Name><Value>123</Value></xml>"
        result = parse_xml(xml)
        assert result["Name"] == "test"
        assert result["Value"] == "123"

    def test_empty_tag(self):
        xml = "<xml><Empty></Empty></xml>"
        result = parse_xml(xml)
        assert result["Empty"] == ""


class TestExtractEncrypt:

    def test_extract_encrypt(self):
        xml = """<xml>
            <ToUserName><![CDATA[corp_id]]></ToUserName>
            <Encrypt><![CDATA[encrypted_data_here]]></Encrypt>
            <AgentID><![CDATA[1000001]]></AgentID>
        </xml>"""
        assert extract_encrypt_from_xml(xml) == "encrypted_data_here"

    def test_missing_encrypt_raises(self):
        xml = "<xml><ToUserName>test</ToUserName></xml>"
        with pytest.raises(ValueError, match="Missing Encrypt"):
            extract_encrypt_from_xml(xml)


class TestParseTextMessage:

    def test_text_message(self):
        xml = """<xml>
            <MsgId>12345</MsgId>
            <FromUserName><![CDATA[user001]]></FromUserName>
            <ToUserName><![CDATA[corp001]]></ToUserName>
            <MsgType><![CDATA[text]]></MsgType>
            <Content><![CDATA[我想找工作]]></Content>
            <CreateTime>1609459200</CreateTime>
        </xml>"""
        msg = parse_message(xml)

        assert isinstance(msg, WeComMessage)
        assert msg.msg_id == "12345"
        assert msg.from_user == "user001"
        assert msg.to_user == "corp001"
        assert msg.msg_type == "text"
        assert msg.content == "我想找工作"
        assert msg.media_id == ""
        assert msg.create_time == 1609459200


class TestParseImageMessage:

    def test_image_message(self):
        xml = """<xml>
            <MsgId>67890</MsgId>
            <FromUserName><![CDATA[user002]]></FromUserName>
            <ToUserName><![CDATA[corp001]]></ToUserName>
            <MsgType><![CDATA[image]]></MsgType>
            <PicUrl><![CDATA[https://example.com/pic.jpg]]></PicUrl>
            <MediaId><![CDATA[media_abc123]]></MediaId>
            <CreateTime>1609459300</CreateTime>
        </xml>"""
        msg = parse_message(xml)

        assert msg.msg_type == "image"
        assert msg.media_id == "media_abc123"
        assert msg.content == "https://example.com/pic.jpg"


class TestParseEventMessage:

    def test_event_message(self):
        xml = """<xml>
            <MsgId></MsgId>
            <FromUserName><![CDATA[user003]]></FromUserName>
            <ToUserName><![CDATA[corp001]]></ToUserName>
            <MsgType><![CDATA[event]]></MsgType>
            <Event><![CDATA[subscribe]]></Event>
            <CreateTime>1609459400</CreateTime>
        </xml>"""
        msg = parse_message(xml)

        assert msg.msg_type == "event"
        assert msg.content == "subscribe"


class TestUnifiedMessageFieldsComplete:
    """验证统一消息对象包含所有必要字段。"""

    def test_all_required_fields_present(self):
        msg = WeComMessage()
        assert hasattr(msg, "msg_id")
        assert hasattr(msg, "from_user")
        assert hasattr(msg, "to_user")
        assert hasattr(msg, "msg_type")
        assert hasattr(msg, "content")
        assert hasattr(msg, "media_id")
        assert hasattr(msg, "create_time")

    def test_default_values(self):
        msg = WeComMessage()
        assert msg.msg_id == ""
        assert msg.from_user == ""
        assert msg.to_user == ""
        assert msg.msg_type == ""
        assert msg.content == ""
        assert msg.media_id == ""
        assert msg.create_time == 0


class TestMissingFieldsBehavior:
    """缺失字段时行为可预测。"""

    def test_missing_content_in_text(self):
        xml = """<xml>
            <MsgId>111</MsgId>
            <FromUserName><![CDATA[user]]></FromUserName>
            <ToUserName><![CDATA[corp]]></ToUserName>
            <MsgType><![CDATA[text]]></MsgType>
            <CreateTime>1000</CreateTime>
        </xml>"""
        msg = parse_message(xml)
        assert msg.content == ""  # 不会报错，返回空字符串

    def test_missing_media_id_in_image(self):
        xml = """<xml>
            <MsgId>222</MsgId>
            <FromUserName><![CDATA[user]]></FromUserName>
            <ToUserName><![CDATA[corp]]></ToUserName>
            <MsgType><![CDATA[image]]></MsgType>
            <CreateTime>2000</CreateTime>
        </xml>"""
        msg = parse_message(xml)
        assert msg.media_id == ""
