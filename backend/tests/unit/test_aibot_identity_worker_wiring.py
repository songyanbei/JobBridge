from app.services import worker


class _FakeIdentityClient:
    def __init__(self):
        self.used = False

    def batch_openuserid_to_userid(self, values):
        self.used = True
        raise AssertionError("not called in wiring test")

    def is_canonical_user_visible(self, userid):
        return True, "visible"


def test_worker_wiring_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(worker.settings, "identity_resolution_enabled", False)
    service = worker.build_aibot_identity_service()
    assert service.client is None


def test_worker_wiring_injects_identity_client_and_directory_verifier(monkeypatch):
    fake = _FakeIdentityClient()
    monkeypatch.setattr(worker.settings, "identity_resolution_enabled", True)
    monkeypatch.setattr(worker, "_AIBOT_IDENTITY_CLIENT", fake)
    service = worker.build_aibot_identity_service()
    assert service.client is fake
    assert service.verify_plain_userid("canonical-a") == (True, "visible")
