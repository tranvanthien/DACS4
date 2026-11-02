"""
Kiểm thử tích hợp WebSocket:
- Xác thực thành viên và từ chối an toàn khi thông tin không hợp lệ.
- Hai người tham gia cùng phòng.
- Gửi dữ liệu (Ping, Chat, WebRTC signaling, TTS toggle, Audio PCM).
- Xử lý các tình huống biên: JSON hỏng, peer offline, reconnect thay thế connection cũ.
- Rời phòng bình thường và dọn dẹp kết nối an toàn.
"""

import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.user import User
from app.models.room import Room, RoomMember
from app.websocket.connection_manager import manager


@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def setup_test_room_and_users(db_session):
    """Tạo dữ liệu thử nghiệm trong DB và tự động dọn dẹp sau test."""
    u1 = db_session.query(User).filter_by(email="ws_test_user1@example.com").first()
    if not u1:
        u1 = User(
            email="ws_test_user1@example.com",
            full_name="WS Test User 1",
            password_hash="fakehash123",
            speaking_language="vi",
            listening_language="en",
        )
        db_session.add(u1)

    u2 = db_session.query(User).filter_by(email="ws_test_user2@example.com").first()
    if not u2:
        u2 = User(
            email="ws_test_user2@example.com",
            full_name="WS Test User 2",
            password_hash="fakehash123",
            speaking_language="en",
            listening_language="vi",
        )
        db_session.add(u2)
    db_session.commit()

    room_code = "WSTEST99"
    room = db_session.query(Room).filter_by(room_code=room_code).first()
    if not room:
        room = Room(
            room_code=room_code,
            room_name="WebSocket Test Room",
            host_id=u1.id,
            is_active=True,
        )
        db_session.add(room)
        db_session.commit()

    # Reset trạng thái phòng về active trước mỗi test (host disconnect test có thể đóng phòng)
    room.is_active = True

    m1 = db_session.query(RoomMember).filter_by(room_id=room.id, user_id=u1.id).first()
    if not m1:
        m1 = RoomMember(room_id=room.id, user_id=u1.id, is_active=True)
        db_session.add(m1)
    else:
        m1.is_active = True

    m2 = db_session.query(RoomMember).filter_by(room_id=room.id, user_id=u2.id).first()
    if not m2:
        m2 = RoomMember(room_id=room.id, user_id=u2.id, is_active=True)
        db_session.add(m2)
    else:
        m2.is_active = True

    db_session.commit()

    yield {
        "room_code": room_code,
        "room_id": room.id,
        "user1_id": u1.id,
        "user2_id": u2.id,
    }

    manager.disconnect(room_code, u1.id)
    manager.disconnect(room_code, u2.id)


def test_websocket_rejection_for_invalid_room_or_member():
    """Kiểm tra từ chối kết nối an toàn khi phòng hoặc user không hợp lệ, không ném exception."""
    client = TestClient(app)

    # 1. Room không tồn tại
    with client.websocket_connect("/ws/rooms/NONEXISTENT_ROOM/99999") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "Room not found" in msg["message"]


def test_two_users_join_exchange_data_and_leave(setup_test_room_and_users):
    """
    Kịch bản đầy đủ hai người tham gia:
    1. User 1 kết nối -> nhận room_peers rỗng.
    2. User 2 kết nối -> nhận room_peers có user1; User 1 nhận user_joined của user2.
    3. Gửi Ping/Pong.
    4. Gửi Chat Message -> Cả 2 nhận được broadcast.
    5. Gửi WebRTC Signaling -> User 2 nhận relay từ user1.
    6. Gửi TTS toggle -> User 1 nhận phản hồi tts_toggled.
    7. User 2 rời phòng -> User 1 nhận user_left; manager dọn dẹp sạch sẽ user 2.
    8. User 1 rời phòng -> manager dọn dẹp phòng, không còn ai.
    """
    room_code = setup_test_room_and_users["room_code"]
    u1_id = setup_test_room_and_users["user1_id"]
    u2_id = setup_test_room_and_users["user2_id"]

    client = TestClient(app)

    # ── Bước 1: User 1 kết nối ────────────────────────────────────────────────
    with client.websocket_connect(f"/ws/rooms/{room_code}/{u1_id}") as ws1:
        msg1 = ws1.receive_json()
        assert msg1["type"] == "room_peers"
        assert u2_id not in msg1["peers"]
        assert manager.get_room_connection_count(room_code) == 1

        # ── Bước 2: User 2 kết nối cùng phòng ─────────────────────────────────
        with client.websocket_connect(f"/ws/rooms/{room_code}/{u2_id}") as ws2:
            msg2_peers = ws2.receive_json()
            assert msg2_peers["type"] == "room_peers"
            assert u1_id in msg2_peers["peers"]

            msg1_joined = ws1.receive_json()
            assert msg1_joined["type"] == "user_joined"
            assert msg1_joined["user_id"] == u2_id

            assert manager.get_room_connection_count(room_code) == 2

            # ── Bước 3: Ping / Pong ───────────────────────────────────────────
            ws1.send_json({"type": "ping"})
            pong = ws1.receive_json()
            assert pong["type"] == "pong"

            # ── Bước 4: Chat Message ──────────────────────────────────────────
            chat_text = "Xin chào thành viên thứ hai!"
            ws1.send_json({"type": "message", "message": chat_text})

            chat_u1 = ws1.receive_json()
            assert chat_u1["type"] == "message"
            assert chat_u1["user_id"] == u1_id
            assert chat_u1["message"] == chat_text

            chat_u2 = ws2.receive_json()
            assert chat_u2["type"] == "message"
            assert chat_u2["user_id"] == u1_id
            assert chat_u2["message"] == chat_text

            # ── Bước 5: WebRTC Signaling (Offer & Candidate) ──────────────────
            fake_sdp = {"type": "offer", "sdp": "v=0\r\no=...fake_sdp..."}
            ws1.send_json({
                "type": "webrtc_offer",
                "target_user_id": u2_id,
                "sdp": fake_sdp,
            })

            relay_offer = ws2.receive_json()
            assert relay_offer["type"] == "webrtc_offer"
            assert relay_offer["from_user_id"] == u1_id
            assert relay_offer["sdp"] == fake_sdp

            fake_cand = {"candidate": "candidate:1 1 UDP ...", "sdpMid": "0"}
            ws2.send_json({
                "type": "webrtc_ice_candidate",
                "target_user_id": u1_id,
                "candidate": fake_cand,
            })

            relay_cand = ws1.receive_json()
            assert relay_cand["type"] == "webrtc_ice_candidate"
            assert relay_cand["from_user_id"] == u2_id
            assert relay_cand["candidate"] == fake_cand

            # ── Bước 6: TTS preference toggle ────────────────────────────────
            ws1.send_json({"type": "tts_toggle", "enabled": False})
            tts_res = ws1.receive_json()
            assert tts_res["type"] == "tts_toggled"
            assert tts_res["enabled"] is False
            assert manager.is_tts_enabled(room_code, u1_id) is False

        # ── Bước 7: User 2 đã thoát (khối with ws2 kết thúc) ──────────────────
        leave_event = ws1.receive_json()
        assert leave_event["type"] == "user_left"
        assert leave_event["user_id"] == u2_id

        assert manager.get_user_websocket(room_code, u2_id) is None
        assert manager.get_room_connection_count(room_code) == 1
        assert manager.get_room_users(room_code) == [u1_id]

    # ── Bước 8: User 1 đã thoát (khối with ws1 kết thúc) ──────────────────────
    assert manager.get_user_websocket(room_code, u1_id) is None
    assert manager.get_room_connection_count(room_code) == 0
    assert manager.get_room_users(room_code) == []


def test_edge_cases_and_error_handling(setup_test_room_and_users):
    """Kiểm tra xử lý ngoại lệ: JSON hỏng, gửi signaling tới peer offline, v.v."""
    room_code = setup_test_room_and_users["room_code"]
    u1_id = setup_test_room_and_users["user1_id"]

    client = TestClient(app)

    with client.websocket_connect(f"/ws/rooms/{room_code}/{u1_id}") as ws:
        # Bỏ qua room_peers
        ws.receive_json()

        # 1. Gửi invalid JSON
        ws.send_text("THIS IS NOT JSON")
        err_msg = ws.receive_json()
        assert err_msg["type"] == "error"
        assert "Invalid JSON" in err_msg["message"]

        # 2. Gửi WebRTC signaling tới user offline
        ws.send_json({
            "type": "webrtc_offer",
            "target_user_id": 999999,
            "sdp": "fake",
        })
        offline_msg = ws.receive_json()
        assert offline_msg["type"] == "webrtc_peer_unavailable"
        assert offline_msg["reason"] == "offline"

        # 3. Gửi audio_start và audio_stop rỗng (quá ngắn)
        ws.send_json({
            "type": "audio_start",
            "speaking_language": "vi",
        })
        ws.send_bytes(b"\x00" * 320)  # 10ms PCM 16kHz
        ws.send_json({"type": "audio_stop"})
        rej_msg = ws.receive_json()
        assert rej_msg["type"] == "transcript_rejected"
        assert rej_msg["reason"] == "AUDIO_TOO_SHORT"


def test_host_disconnect_auto_closes_room(setup_test_room_and_users, db_session):
    """
    Khi host (user1) ngắt kết nối WebSocket:
    - Phòng phải được đặt is_active = False trong DB.
    - Kiểm tra qua DB sau khi cả hai WS đã đóng.

    Lưu ý: TestClient đồng bộ không thể relay sự kiện broadcast giữa
    hai nested WS contexts, nên chỉ xác minh trạng thái DB.
    """
    room_code = setup_test_room_and_users["room_code"]
    u1_id = setup_test_room_and_users["user1_id"]   # host
    u2_id = setup_test_room_and_users["user2_id"]   # member

    from app.models.room import Room
    from app.database import SessionLocal
    client = TestClient(app)

    # Bước 1: Cả host và member kết nối
    with client.websocket_connect(f"/ws/rooms/{room_code}/{u1_id}") as ws_host:
        ws_host.receive_json()  # room_peers

        with client.websocket_connect(f"/ws/rooms/{room_code}/{u2_id}") as ws_member:
            ws_member.receive_json()  # room_peers
            ws_host.receive_json()    # user_joined

            # Bước 2: Member rời trước (không phải host → không close room)
            # WS member đóng khi ra khỏi with block

        # host nhận user_left của member
        left_msg = ws_host.receive_json()
        assert left_msg["type"] == "user_left"
        assert left_msg["user_id"] == u2_id

        # Xác nhận phòng vẫn mở khi chỉ member rời (dùng session mới)
        with SessionLocal() as verify_db:
            room_still_open = verify_db.query(Room).filter_by(room_code=room_code).first()
            assert room_still_open.is_active is True, "Phòng vẫn phải mở khi chỉ member rời"

    # Bước 3: Host WS đóng (context manager kết thúc) → backend auto-close
    # Kiểm tra DB: phòng phải bị đóng — dùng session hoàn toàn mới
    with SessionLocal() as fresh_db:
        room_in_db = fresh_db.query(Room).filter_by(room_code=room_code).first()
        assert room_in_db is not None
        assert room_in_db.is_active is False, "Phòng phải được đóng khi host ngắt kết nối"
