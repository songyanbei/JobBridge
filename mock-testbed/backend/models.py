"""Mock 企业微信测试台 · 轻量 ORM。

只映射主后端 user / wecom_inbound_event 两张表所需的字段。
不 import 主后端 app.models，保持沙箱独立。
字段名、类型、enum 值必须与主后端 backend/app/models.py 保持一致。
"""
from sqlalchemy import BigInteger, Column, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects import mysql

from db import Base


class MockUser(Base):
    """user 表 —— 只读 3 个字段，用于展示可选身份列表。"""

    __tablename__ = "user"

    external_userid = Column(String(64), primary_key=True)
    role = Column(
        Enum("worker", "factory", "broker", name="user_role"),
        nullable=False,
    )
    display_name = Column(String(64), nullable=True)


class MockWecomInboundEvent(Base):
    """wecom_inbound_event —— 入站消息落库。

    字段和主后端 models.WecomInboundEvent 完全对齐；沙箱只使用基础字段
    （msg_id / from_userid / msg_type / media_id / content_brief / status / created_at）。
    """

    __tablename__ = "wecom_inbound_event"

    id = Column(BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True)
    msg_id = Column(String(64), nullable=False, unique=True)
    from_userid = Column(String(64), nullable=False)
    msg_type = Column(
        Enum(
            "text", "image", "voice",
            "video", "file", "link", "location",
            "event", "other",
            name="wecom_msg_type",
        ),
        nullable=False,
    )
    media_id = Column(String(128), nullable=True)
    content_brief = Column(String(500), nullable=True)
    status = Column(
        Enum("received", "processing", "done", "failed", "dead_letter", name="wecom_event_status"),
        nullable=False,
        server_default="received",
    )
    retry_count = Column(Integer, nullable=False, server_default="0")
    worker_started_at = Column(DateTime, nullable=True)
    worker_finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
