"""对象存储工厂。

业务层统一通过 get_storage() 获取存储实例，
不允许直接 import 具体后端实现。
"""
from app.config import settings
from app.storage.base import StorageBackend

_PROVIDER_REGISTRY: dict[str, type[StorageBackend]] = {}


def _ensure_registry() -> None:
    """延迟注册 provider，避免 import 环路。"""
    if _PROVIDER_REGISTRY:
        return

    from app.storage.local import LocalStorage

    _PROVIDER_REGISTRY["local"] = LocalStorage


def get_storage(provider: str | None = None) -> StorageBackend:
    """获取存储后端实例。

    Args:
        provider: 指定 provider 名称，为 None 时读取 settings.oss_provider。

    Raises:
        ValueError: 未知 provider。
    """
    _ensure_registry()
    name = provider or settings.oss_provider
    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown storage provider: '{name}'. "
            f"Available: {sorted(_PROVIDER_REGISTRY.keys())}"
        )
    return cls()
