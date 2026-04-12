"""对象存储抽象层。

一期使用本地文件系统（LocalStorage），后期可切换 MinIO / 阿里 OSS / 腾讯 COS。
业务代码只依赖 StorageBackend ABC，通过 storage/__init__.py 的工厂函数获取实例。
"""
from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """对象存储后端抽象接口。"""

    @abstractmethod
    def save(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """保存文件。

        Args:
            key: 存储键（如 "jobs/123/photo_1.jpg"）
            data: 文件二进制内容
            content_type: MIME 类型

        Returns:
            可访问的 URL 或路径
        """
        ...

    @abstractmethod
    def get_url(self, key: str) -> str:
        """获取文件访问 URL。"""
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除文件，返回是否成功。"""
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """检查文件是否存在。"""
        ...
