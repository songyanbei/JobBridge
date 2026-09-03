from pathlib import Path


def test_legacy_demo_cleanup_script_is_explicitly_disabled_and_non_destructive():
    script = (Path(__file__).parents[2].parent / "scripts" / "demo_env_cleanup.sh").read_text(
        encoding="utf-8"
    ).lower()

    assert "已停用" in script
    assert "exit 2" in script
    assert "delete from" not in script
    assert "redis-cli del" not in script
    assert "queue:incoming" not in script
    assert "like 'demo_" not in script
    assert "/admin/demo/{demo_id}/preview" in script
