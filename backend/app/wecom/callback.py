"""企微回调消息解析。

负责将企微 XML 回调解析为统一消息对象，至少覆盖文本和图片消息。
不包含业务分发逻辑。
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class WeComMessage:
    """企微统一消息对象。

    覆盖一期范围内的文本和图片消息的所有必要字段。
    """
    msg_id: str = ""
    from_user: str = ""
    to_user: str = ""
    msg_type: str = ""
    content: str = ""
    media_id: str = ""
    create_time: int = 0


def parse_xml(xml_text: str) -> dict:
    """将 XML 文本解析为字典。

    Args:
        xml_text: 企微回调的 XML 文本。

    Returns:
        XML 各节点的 tag -> text 字典。
    """
    root = ET.fromstring(xml_text)
    result = {}
    for child in root:
        result[child.tag] = child.text or ""
    return result


def extract_encrypt_from_xml(xml_text: str) -> str:
    """从回调 XML 中提取 Encrypt 字段。

    企微回调 XML 格式:
    <xml>
        <ToUserName>...</ToUserName>
        <Encrypt>...</Encrypt>
        <AgentID>...</AgentID>
    </xml>
    """
    parsed = parse_xml(xml_text)
    encrypt = parsed.get("Encrypt", "")
    if not encrypt:
        raise ValueError("Missing Encrypt field in callback XML")
    return encrypt


def parse_message(xml_text: str) -> WeComMessage:
    """将解密后的明文 XML 解析为统一消息对象。

    支持的消息类型:
    - text: 文本消息
    - image: 图片消息

    Args:
        xml_text: 解密后的明文 XML。

    Returns:
        WeComMessage 统一消息对象。
    """
    parsed = parse_xml(xml_text)

    msg_type = parsed.get("MsgType", "")

    msg = WeComMessage(
        msg_id=parsed.get("MsgId", ""),
        from_user=parsed.get("FromUserName", ""),
        to_user=parsed.get("ToUserName", ""),
        msg_type=msg_type,
        create_time=int(parsed.get("CreateTime", "0") or "0"),
    )

    if msg_type == "text":
        msg.content = parsed.get("Content", "")
    elif msg_type == "image":
        msg.media_id = parsed.get("MediaId", "")
        msg.content = parsed.get("PicUrl", "")
    elif msg_type == "voice":
        msg.media_id = parsed.get("MediaId", "")
    elif msg_type == "event":
        msg.content = parsed.get("Event", "")

    return msg
