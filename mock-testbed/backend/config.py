"""Mock 企业微信测试台 · 配置。

独立于主后端的 pydantic settings，env 前缀 MOCK_。
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    """沙箱后端配置。所有字段通过 MOCK_ 前缀的 env 覆盖。"""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    db_dsn: str = Field(
        default="mysql+pymysql://root:password@localhost:3306/jobbridge",
        alias="MOCK_DB_DSN",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        alias="MOCK_REDIS_URL",
    )
    port: int = Field(default=8001, alias="MOCK_PORT")

    # 默认只绑本机 loopback；演示时若需 LAN 访问，export MOCK_HOST=0.0.0.0
    host: str = Field(default="127.0.0.1", alias="MOCK_HOST")

    # 逗号分隔的 CORS origin 列表
    cors_origins: str = Field(
        default="http://localhost:5174,http://127.0.0.1:5174",
        alias="MOCK_CORS_ORIGINS",
    )

    # 纯演示字段（不会参与真实鉴权）
    corpid: str = Field(default="wwmock_corpid", alias="MOCK_CORPID")
    agentid: str = Field(default="1000002", alias="MOCK_AGENTID")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# 模块级单例
settings = MockSettings()
