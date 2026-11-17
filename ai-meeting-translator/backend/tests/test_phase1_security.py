"""
Comprehensive Phase 1 Security Test Suite:
1. JWT Authentication (Issue token, verify token on APIs, eliminate user_id/host_id trust)
2. WebSocket Protection (Verify JWT token before accept, prevent user_id spoofing, prevent unauthorized connection)
3. Admin Protection (Do not leak ADMIN_SECRET_KEY, verify role/token, require admin privilege)
4. CORS (Origin list check, disallow wildcard)
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.models.room import Room, RoomMember
from app.models.chat import ChatMessage
from app.services.user_service import hash_password
from app.core.security import create_access_token, decode_access_token
from app.config import settings

# In-memory SQLite for isolated, fast, robust testing
TEST_DATABASE_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()

    # Tạo User A
    user_a = User(
        full_name="User Alpha",
        email="user_a@example.com",
        password_hash=hash_password("password_a_123"),
        speaking_language="vi",
        listening_language="en",
        is_admin=False,
    )
    # Tạo User B
    user_b = User(
        full_name="User Beta",
        email="user_b@example.com",
        password_hash=hash_password("password_b_123"),
        speaking_language="en",
        listening_language="vi",
        is_admin=False,
    )
    # Tạo Admin User
    admin_user = User(
        full_name="Admin Master",
        email="admin_master@example.com",
        password_hash=hash_password("admin_secret_pwd"),
        speaking_language="vi",
        listening_language="vi",
        is_admin=True,
    )
    db.add_all([user_a, user_b, admin_user])
    db.commit()
    db.refresh(user_a)
    db.refresh(user_b)
    db.refresh(admin_user)

    # User A tạo 1 phòng họp
    room_a = Room(
        room_code="ROOM_A",
        room_name="Phòng họp Alpha",
        host_id=user_a.id,
        is_active=True,
        meeting_mode="normal",
    )
    db.add(room_a)
    db.commit()
    db.refresh(room_a)

    member_a = RoomMember(room_id=room_a.id, user_id=user_a.id, is_active=True)
    db.add(member_a)

    # User A gửi 1 tin nhắn vào phòng
    msg_a = ChatMessage(room_id=room_a.id, user_id=user_a.id, content="Tin nhắn mật của phòng Alpha")
    db.add(msg_a)
    db.commit()

    yield {
        "user_a_id": user_a.id,
        "user_b_id": user_b.id,
        "admin_id": admin_user.id,
        "room_a_code": room_a.room_code,
    }
    db.close()
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════════════
# 1. KIỂM THỬ XÁC THỰC JWT & ĐĂNG NHẬP
# ══════════════════════════════════════════════════════════════════════════════

def test_login_success_issues_jwt_token(client):
    """Đăng nhập thành công trả về access_token chuẩn JWT Bearer."""
    res = client.post("/api/auth/login", json={
        "email": "user_a@example.com",
        "password": "password_a_123"
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "user_a@example.com"

    # Giải mã kiểm tra claims
    payload = decode_access_token(data["access_token"])
    assert payload["email"] == "user_a@example.com"
    assert payload["is_admin"] is False


def test_login_failure_invalid_credentials(client):
    """Đăng nhập sai mật khẩu bị từ chối 401."""
    res = client.post("/api/auth/login", json={
        "email": "user_a@example.com",
        "password": "wrong_password"
    })
    assert res.status_code == 401


def test_auth_me_endpoint_requires_token(client):
    """API /api/auth/me yêu cầu token hợp lệ."""
    # Không có token -> 401
    res = client.get("/api/auth/me")
    assert res.status_code == 401

    # Đăng nhập lấy token
    login_res = client.post("/api/auth/login", json={
        "email": "user_a@example.com",
        "password": "password_a_123"
    })
    token = login_res.json()["access_token"]

    # Có token -> 200
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["email"] == "user_a@example.com"


# ══════════════════════════════════════════════════════════════════════════════
# 2. KIỂM THỬ BẢO VỆ REST APIS & CHỐNG GIẢ MẠO USER_ID / HOST_ID
# ══════════════════════════════════════════════════════════════════════════════

def test_create_room_uses_token_identity(client, setup_test_db):
    """Tạo phòng sử dụng host_id từ token, không tin tưởng host_id do client gửi."""
    login_a = client.post("/api/auth/login", json={"email": "user_a@example.com", "password": "password_a_123"})
    token_a = login_a.json()["access_token"]
    user_b_id = setup_test_db["user_b_id"]

    # Client gửi kèm host_id của User B nhằm mạo danh
    res = client.post(
        f"/api/rooms?host_id={user_b_id}",
        json={"room_name": "Phòng của A nhưng giả B"},
        headers={"Authorization": f"Bearer {token_a}"}
    )
    assert res.status_code == 201
    room = res.json()["room"]
    # Backend phải gán host_id = User A (từ token), không phải User B!
    assert room["host_id"] == setup_test_db["user_a_id"]


def test_user_b_cannot_close_user_a_room(client, setup_test_db):
    """User B không thể đóng phòng do User A làm chủ."""
    login_b = client.post("/api/auth/login", json={"email": "user_b@example.com", "password": "password_b_123"})
    token_b = login_b.json()["access_token"]
    user_a_id = setup_test_db["user_a_id"]
    room_code = setup_test_db["room_a_code"]

    # User B cố tình gửi request đóng phòng với host_id của User A
    res = client.post(
        f"/api/rooms/{room_code}/close?host_id={user_a_id}",
        headers={"Authorization": f"Bearer {token_b}"}
    )
    assert res.status_code == 403
    assert "Chỉ host mới được đóng phòng" in res.json()["detail"]


def test_user_b_cannot_change_user_a_room_mode(client, setup_test_db):
    """User B không thể đổi chế độ phòng của User A."""
    login_b = client.post("/api/auth/login", json={"email": "user_b@example.com", "password": "password_b_123"})
    token_b = login_b.json()["access_token"]
    room_code = setup_test_db["room_a_code"]

    res = client.put(
        f"/api/rooms/{room_code}/mode",
        json={"meeting_mode": "ai"},
        headers={"Authorization": f"Bearer {token_b}"}
    )
    assert res.status_code == 403


def test_user_b_cannot_view_messages_without_membership(client, setup_test_db):
    """User B chưa tham gia phòng Alpha thì không được xem tin nhắn phòng."""
    login_b = client.post("/api/auth/login", json={"email": "user_b@example.com", "password": "password_b_123"})
    token_b = login_b.json()["access_token"]
    room_code = setup_test_db["room_a_code"]

    # User B gửi user_id=user_a_id hòng giả mạo
    res = client.get(
        f"/api/rooms/{room_code}/messages?user_id={setup_test_db['user_a_id']}",
        headers={"Authorization": f"Bearer {token_b}"}
    )
    assert res.status_code == 403
    assert "Bạn không phải thành viên phòng này" in res.json()["detail"]


def test_user_b_cannot_change_user_a_language_or_password(client, setup_test_db):
    """User B không thể sửa ngôn ngữ hoặc mật khẩu của User A."""
    login_b = client.post("/api/auth/login", json={"email": "user_b@example.com", "password": "password_b_123"})
    token_b = login_b.json()["access_token"]
    user_a_id = setup_test_db["user_a_id"]

    # Thử đổi ngôn ngữ của A
    res_lang = client.put(
        f"/api/users/{user_a_id}/languages",
        json={"speaking_language": "ja", "listening_language": "fr"},
        headers={"Authorization": f"Bearer {token_b}"}
    )
    assert res_lang.status_code == 403

    # Thử đổi mật khẩu của A
    res_pwd = client.put(
        f"/api/users/{user_a_id}/password",
        json={"current_password": "password_a_123", "new_password": "hacked_password_123"},
        headers={"Authorization": f"Bearer {token_b}"}
    )
    assert res_pwd.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# 3. KIỂM THỬ BẢO VỆ WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════

def test_websocket_rejects_missing_token(client, setup_test_db):
    """Kết nối WebSocket không có token bị từ chối an toàn."""
    room_code = setup_test_db["room_a_code"]
    user_id = setup_test_db["user_a_id"]

    with client.websocket_connect(f"/ws/rooms/{room_code}/{user_id}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "Thiếu token xác thực" in msg["message"]


def test_websocket_rejects_spoofed_user_id(client, setup_test_db):
    """Kết nối WebSocket với token của User B nhưng mạo danh user_id của User A bị từ chối."""
    login_b = client.post("/api/auth/login", json={"email": "user_b@example.com", "password": "password_b_123"})
    token_b = login_b.json()["access_token"]
    room_code = setup_test_db["room_a_code"]
    user_a_id = setup_test_db["user_a_id"]

    with client.websocket_connect(f"/ws/rooms/{room_code}/{user_a_id}?token={token_b}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "user_id không khớp với token" in msg["message"]


def test_websocket_accepts_valid_token(client, setup_test_db):
    """Kết nối WebSocket với token hợp lệ của chính chủ được chấp nhận."""
    login_a = client.post("/api/auth/login", json={"email": "user_a@example.com", "password": "password_a_123"})
    token_a = login_a.json()["access_token"]
    room_code = setup_test_db["room_a_code"]
    user_a_id = setup_test_db["user_a_id"]

    with client.websocket_connect(f"/ws/rooms/{room_code}/{user_a_id}?token={token_a}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "room_mode"


# ══════════════════════════════════════════════════════════════════════════════
# 4. KIỂM THỬ BẢO VỆ ADMIN & PHÂN QUYỀN RIÊNG
# ══════════════════════════════════════════════════════════════════════════════

def test_admin_login_does_not_leak_server_secret_key(client):
    """Đăng nhập Admin thành công trả về access_token, TUYỆT ĐỐI không trả admin_key."""
    res = client.post("/api/admin/login", json={
        "email": "admin_master@example.com",
        "password": "admin_secret_pwd"
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    # Kiểm tra không lộ secret key
    assert "admin_key" not in data


def test_normal_user_cannot_access_admin_api(client):
    """User thường có JWT token nhưng không có is_admin bị từ chối 403 khi vào Admin API."""
    login_a = client.post("/api/auth/login", json={"email": "user_a@example.com", "password": "password_a_123"})
    token_a = login_a.json()["access_token"]

    res = client.get("/api/admin/stats/overview", headers={"Authorization": f"Bearer {token_a}"})
    assert res.status_code == 403
    assert "Quản trị viên" in res.json()["detail"]


def test_admin_user_can_access_admin_api_with_jwt(client):
    """Admin đăng nhập bằng JWT Bearer token truy cập thành công vào Admin API."""
    login_admin = client.post("/api/admin/login", json={
        "email": "admin_master@example.com",
        "password": "admin_secret_pwd"
    })
    admin_token = login_admin.json()["access_token"]

    res = client.get("/api/admin/stats/overview", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    assert "users" in res.json()
    assert res.json()["users"]["total"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 5. KIỂM THỬ CẤU HÌNH CORS
# ══════════════════════════════════════════════════════════════════════════════

def test_cors_config_no_wildcard():
    """Kiểm tra cấu hình CORS không sử dụng wildcard '*'."""
    origins = settings.cors_origins_list
    assert "*" not in origins
    assert len(origins) > 0
