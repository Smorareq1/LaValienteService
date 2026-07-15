from src.core.security import hash_opaque_token, hash_password, verify_password


def test_password_hash_is_verifiable() -> None:
    hashed_password = hash_password("Correct-Horse-Battery-Staple")

    assert verify_password("Correct-Horse-Battery-Staple", hashed_password)
    assert not verify_password("incorrect-password", hashed_password)


def test_opaque_token_hash_is_deterministic() -> None:
    assert hash_opaque_token("token-value") == hash_opaque_token("token-value")
    assert hash_opaque_token("token-value") != hash_opaque_token("another-token")
