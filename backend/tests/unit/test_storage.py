"""对象存储测试：保存、URL 生成、删除、存在性检查、key 路径规则。"""
import os
import tempfile
import pytest

from app.storage.local import LocalStorage
from app.storage import get_storage, _PROVIDER_REGISTRY
from app.storage.base import StorageBackend


@pytest.fixture(autouse=True)
def clear_registry():
    _PROVIDER_REGISTRY.clear()
    yield
    _PROVIDER_REGISTRY.clear()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def storage(tmp_dir):
    return LocalStorage(base_dir=tmp_dir, base_url="/files")


class TestLocalStorageSave:

    def test_save_creates_file(self, storage, tmp_dir):
        url = storage.save("jobs/123/abc.jpg", b"image data")
        path = os.path.join(tmp_dir, "jobs", "123", "abc.jpg")
        assert os.path.isfile(path)

    def test_save_returns_url(self, storage):
        url = storage.save("jobs/123/abc.jpg", b"data")
        assert url == "/files/jobs/123/abc.jpg"

    def test_save_creates_intermediate_dirs(self, storage, tmp_dir):
        storage.save("resumes/456/def.pdf", b"pdf data")
        assert os.path.isdir(os.path.join(tmp_dir, "resumes", "456"))

    def test_save_content_is_correct(self, storage, tmp_dir):
        storage.save("test/1/file.txt", b"hello world")
        path = os.path.join(tmp_dir, "test", "1", "file.txt")
        with open(path, "rb") as f:
            assert f.read() == b"hello world"


class TestLocalStorageGetUrl:

    def test_get_url(self, storage):
        url = storage.get_url("avatars/789/photo.png")
        assert url == "/files/avatars/789/photo.png"

    def test_get_url_normalizes_path(self, storage):
        url = storage.get_url("jobs/1/test.jpg")
        assert "//" not in url.replace("//", "", 1)  # no double slashes beyond protocol


class TestLocalStorageDelete:

    def test_delete_existing_file(self, storage, tmp_dir):
        storage.save("test/1/file.txt", b"data")
        assert storage.delete("test/1/file.txt") is True
        assert not os.path.exists(os.path.join(tmp_dir, "test", "1", "file.txt"))

    def test_delete_nonexistent_file(self, storage):
        assert storage.delete("nonexistent/file.txt") is False


class TestLocalStorageExists:

    def test_exists_true(self, storage):
        storage.save("test/1/file.txt", b"data")
        assert storage.exists("test/1/file.txt") is True

    def test_exists_false(self, storage):
        assert storage.exists("nonexistent/file.txt") is False


class TestKeyPathRules:

    def test_key_format_entity_record_uuid(self, storage, tmp_dir):
        """key 遵循 {entity_type}/{record_id}/{filename}.{ext} 格式。"""
        storage.save("jobs/123/abc-def-ghi.jpg", b"data")
        assert os.path.isfile(os.path.join(tmp_dir, "jobs", "123", "abc-def-ghi.jpg"))

    def test_path_traversal_rejected(self, storage):
        with pytest.raises(ValueError, match="Invalid storage key"):
            storage.save("../../../etc/passwd", b"evil")

    def test_absolute_path_rejected(self, storage):
        with pytest.raises(ValueError, match="Invalid storage key"):
            storage.save("/etc/passwd", b"evil")

    def test_windows_absolute_path_rejected(self, storage):
        """Windows 盘符绝对路径不能逃逸出 base_dir。"""
        with pytest.raises(ValueError, match="Invalid storage key"):
            storage.save("C:/temp/evil.txt", b"evil")

    def test_windows_drive_letter_variations(self, storage):
        with pytest.raises(ValueError, match="Invalid storage key"):
            storage.save("D:\\temp\\evil.txt", b"evil")


class TestStorageFactory:

    def test_get_storage_returns_local(self):
        from unittest.mock import patch
        with patch("app.storage.settings") as mock_settings:
            mock_settings.oss_provider = "local"
            s = get_storage()
            assert isinstance(s, LocalStorage)
            assert isinstance(s, StorageBackend)

    def test_get_storage_explicit_local(self):
        s = get_storage(provider="local")
        assert isinstance(s, LocalStorage)

    def test_get_storage_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown storage provider"):
            get_storage(provider="s3")
