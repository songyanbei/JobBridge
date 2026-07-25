from app.core.logging_setup import identifier_hash


def test_identifier_hash_is_stable_and_does_not_expose_raw_value():
    raw = "external-user-sensitive-123"

    hashed = identifier_hash(raw)

    assert hashed == identifier_hash(raw)
    assert len(hashed) == 12
    assert raw not in hashed
    assert identifier_hash("another-user") != hashed
