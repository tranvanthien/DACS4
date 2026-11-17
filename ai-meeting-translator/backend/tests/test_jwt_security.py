"""
Unit tests for JWT creation, expiration and tampering checks.
"""
from datetime import timedelta
import pytest
from fastapi import HTTPException

from app.core.security import create_access_token, decode_access_token


def test_create_and_decode_valid_token():
    payload = {"sub": "42", "email": "test@example.com", "is_admin": False}
    token = create_access_token(payload)
    assert isinstance(token, str)

    decoded = decode_access_token(token)
    assert decoded["sub"] == "42"
    assert decoded["email"] == "test@example.com"
    assert decoded["is_admin"] is False
    assert "exp" in decoded
    assert "iat" in decoded


def test_expired_token_raises_401():
    payload = {"sub": "42"}
    # Tạo token đã hết hạn 10 giây trước
    token = create_access_token(payload, expires_delta=timedelta(seconds=-10))

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(token)

    assert exc_info.value.status_code == 401
    assert "hết hạn" in exc_info.value.detail


def test_tampered_token_raises_401():
    payload = {"sub": "42"}
    token = create_access_token(payload)

    # Làm biến dạng token (thay đổi chữ ký)
    tampered_token = token[:-4] + "fake"

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(tampered_token)

    assert exc_info.value.status_code == 401
    assert "không hợp lệ" in exc_info.value.detail
