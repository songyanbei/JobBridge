"""本地文件系统存储后端。

一期默认实现，文件保存在本地磁盘。
key 模板约定: {entity_type}/{record_id}/{uuid}.{ext}
"""
import os
import logging

from app.config import settings
from app.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class LocalStorage(StorageBackend):
    """本地文件系统存储实现。"""

    def __init__(self, base_dir: str | None = None, base_url: str | None = None):
        self._base_dir = base_dir or getattr(settings, "oss_local_dir", "uploads")
        self._base_url = base_url or getattr(settings, "oss_local_url_prefix", "/files")

    def _full_path(self, key: str) -> str:
        """将 key 转换为完整本地路径，并做路径穿越/逃逸保护。"""
        normalized = os.path.normpath(key).replace("\\", "/")
        if normalized.startswith("..") or normalized.startswith("/"):
            raise ValueError(f"Invalid storage key: '{key}'")

        # 拼接后取绝对路径，再校验结果确实在 base_dir 内
        base = os.path.abspath(self._base_dir)
        full = os.path.abspath(os.path.join(base, normalized))
        if not full.startswith(base + os.sep) and full != base:
            raise ValueError(f"Invalid storage key: '{key}'")

        return full

    def save(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """保存文件到本地磁盘。

        自动创建中间目录。返回可访问的 URL。
        """
        path = self._full_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        logger.info("LocalStorage: saved %s (%d bytes)", key, len(data))
        return self.get_url(key)

    def get_url(self, key: str) -> str:
        """返回文件访问 URL。"""
        normalized = os.path.normpath(key).replace("\\", "/")
        base = self._base_url.rstrip("/")
        return f"{base}/{normalized}"

    def delete(self, key: str) -> bool:
        """删除文件。文件不存在时返回 False。"""
        path = self._full_path(key)
        if not os.path.exists(path):
            return False
        os.remove(path)
        logger.info("LocalStorage: deleted %s", key)
        return True

    def exists(self, key: str) -> bool:
        """检查文件是否存在。"""
        path = self._full_path(key)
        return os.path.isfile(path)
