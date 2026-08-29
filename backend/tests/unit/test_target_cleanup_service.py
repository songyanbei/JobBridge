from app.services.target_cleanup_service import job_cleanup_succeeded

def test_missing_cleanup_task_fails_closed():
    class Q:
        def filter_by(self, **_): return self
        def first(self): return None
    class DB:
        def query(self, *_): return Q()
    assert not job_cleanup_succeeded(DB(), 1)
