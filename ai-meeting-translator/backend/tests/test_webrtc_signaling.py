"""
Test WebRTC Signaling & Multi-stream Flow (Module 09.3)
Kiểm tra luồng signaling giữa 2 peer:
1. Kết nối 2 client vào cùng phòng.
2. Trao đổi WebRTC Offer / Answer (kèm video m-lines).
3. Trao đổi ICE candidate.
4. Gửi webrtc_track_meta và webrtc_track_state (camera + screen share đồng thời).
5. Gửi renegotiation offer khi bật screen share trong khi camera đang chạy.
6. Dừng screen share, camera vẫn giữ nguyên.
7. Xử lý target_user_id kiểu int lẫn string an toàn.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.user import User
from app.models.room import Room, RoomMember
from app.websocket.connection_manager import manager


@pytest.fixture
def setup_webrtc_room():
    db = SessionLocal()
    try:
        u1 = db.query(User).filter_by(email="rtc_user1@example.com").first()
        if not u1:
            u1 = User(email="rtc_user1@example.com", full_name="RTC User 1", password_hash="hash123", speaking_language="vi", listening_language="en")
            db.add(u1)

        u2 = db.query(User).filter_by(email="rtc_user2@example.com").first()
        if not u2:
            u2 = User(email="rtc_user2@example.com", full_name="RTC User 2", password_hash="hash123", speaking_language="en", listening_language="vi")
            db.add(u2)
        db.commit()

        room_code = "RTCMEET1"
        room = db.query(Room).filter_by(room_code=room_code).first()
        if not room:
            room = Room(room_code=room_code, room_name="RTC Test Room", host_id=u1.id, is_active=True)
            db.add(room)
            db.commit()
        else:
            room.is_active = True
            db.commit()

        m1 = db.query(RoomMember).filter_by(room_id=room.id, user_id=u1.id).first()
        if not m1:
            db.add(RoomMember(room_id=room.id, user_id=u1.id, is_active=True))
        else:
            m1.is_active = True

        m2 = db.query(RoomMember).filter_by(room_id=room.id, user_id=u2.id).first()
        if not m2:
            db.add(RoomMember(room_id=room.id, user_id=u2.id, is_active=True))
        else:
            m2.is_active = True
        db.commit()

        yield {
            "room_code": room_code,
            "user1_id": u1.id,
            "user2_id": u2.id,
        }
    finally:
        manager.disconnect("RTCMEET1", u1.id)
        manager.disconnect("RTCMEET1", u2.id)
        db.close()


def test_ice_config_endpoint():
    """Kiểm tra API /api/webrtc/ice-config trả về danh sách ICE servers hợp lệ."""
    client = TestClient(app)
    res = client.get("/api/webrtc/ice-config")
    assert res.status_code == 200
    data = res.json()
    assert "iceServers" in data
    assert len(data["iceServers"]) >= 2
    # Phải có stun server
    stun_found = any("stun" in str(s.get("urls", "")) for s in data["iceServers"])
    assert stun_found


def test_webrtc_full_signaling_and_multitrack_flow(setup_webrtc_room):
    """
    Kịch bản kiểm tra toàn bộ luồng WebRTC giữa 2 người:
    - User 1 và User 2 kết nối.
    - User 1 gửi Offer (chứa video track camera).
    - User 2 nhận Offer và phản hồi Answer.
    - Cả 2 trao đổi ICE candidate (test cả target_user_id int và string).
    - User 1 bật thêm Screen Share đồng thời với Camera.
    - User 1 gửi renegotiate offer cho video track thứ 2 (screen share).
    - User 2 nhận relay và xác nhận camera + screen cùng hoạt động.
    - User 1 dừng Screen Share, gửi track_state: camera=True, screen=False.
    """
    room_code = setup_webrtc_room["room_code"]
    u1_id = setup_webrtc_room["user1_id"]
    u2_id = setup_webrtc_room["user2_id"]

    client = TestClient(app)

    with client.websocket_connect(f"/ws/rooms/{room_code}/{u1_id}") as ws1:
        # ws1 nhận room_peers
        p1 = ws1.receive_json()
        assert p1["type"] == "room_peers"

        with client.websocket_connect(f"/ws/rooms/{room_code}/{u2_id}") as ws2:
            # ws2 nhận room_peers
            p2 = ws2.receive_json()
            assert p2["type"] == "room_peers"
            assert u1_id in p2["peers"]

            # ws1 nhận user_joined của u2
            j1 = ws1.receive_json()
            assert j1["type"] == "user_joined"
            assert j1["user_id"] == u2_id

            # 1. User 1 gửi webrtc_ready
            ws1.send_json({"type": "webrtc_ready", "user_id": u1_id})
            ready_msg = ws2.receive_json()
            assert ready_msg["type"] == "webrtc_ready"
            assert ready_msg["user_id"] == u1_id

            # 2. User 1 gửi Offer (Camera)
            offer_sdp = {
                "type": "offer",
                "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=content:camera\r\n"
            }
            # Test target_user_id dạng int
            ws1.send_json({
                "type": "webrtc_offer",
                "target_user_id": u2_id,
                "sdp": offer_sdp
            })
            offer_rcv = ws2.receive_json()
            assert offer_rcv["type"] == "webrtc_offer"
            assert offer_rcv["from_user_id"] == u1_id
            assert offer_rcv["sdp"] == offer_sdp

            # 3. User 2 gửi Answer
            answer_sdp = {
                "type": "answer",
                "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            }
            # Test target_user_id dạng string (vẫn được backend ép int an toàn)
            ws2.send_json({
                "type": "webrtc_answer",
                "target_user_id": str(u1_id),
                "sdp": answer_sdp
            })
            answer_rcv = ws1.receive_json()
            assert answer_rcv["type"] == "webrtc_answer"
            assert answer_rcv["from_user_id"] == u2_id
            assert answer_rcv["sdp"] == answer_sdp

            # 4. Trao đổi ICE candidate
            cand = {"candidate": "candidate:1 1 UDP 2122260223 192.168.1.100 50000 typ host", "sdpMid": "0"}
            ws1.send_json({
                "type": "webrtc_ice_candidate",
                "target_user_id": str(u2_id),
                "candidate": cand
            })
            cand_rcv = ws2.receive_json()
            assert cand_rcv["type"] == "webrtc_ice_candidate"
            assert cand_rcv["from_user_id"] == u1_id
            assert cand_rcv["candidate"] == cand

            # 5. User 1 bật Screen Share đồng thời với Camera
            # Gửi track meta cho screen
            ws1.send_json({
                "type": "webrtc_track_meta",
                "track_id": "screen-track-123",
                "kind": "screen"
            })
            meta_rcv = ws2.receive_json()
            assert meta_rcv["type"] == "webrtc_track_meta"
            assert meta_rcv["from_user_id"] == u1_id
            assert meta_rcv["track_id"] == "screen-track-123"
            assert meta_rcv["kind"] == "screen"

            # Gửi track state: CẢ CAMERA VÀ SCREEN ĐỀU BẬT
            ws1.send_json({
                "type": "webrtc_track_state",
                "camera": True,
                "screen": True
            })
            state_rcv = ws2.receive_json()
            assert state_rcv["type"] == "webrtc_track_state"
            assert state_rcv["from_user_id"] == u1_id
            assert state_rcv["camera"] is True
            assert state_rcv["screen"] is True

            # 6. Renegotiation Offer chứa cả 2 video streams (camera + screen)
            reneg_offer = {
                "type": "offer",
                "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nm=video 9 UDP/TLS/RTP/SAVPF 97\r\n"
            }
            ws1.send_json({
                "type": "webrtc_offer",
                "target_user_id": u2_id,
                "sdp": reneg_offer
            })
            reneg_rcv = ws2.receive_json()
            assert reneg_rcv["type"] == "webrtc_offer"
            assert reneg_rcv["sdp"] == reneg_offer

            # 7. User 1 dừng Screen Share, nhưng camera vẫn bật!
            ws1.send_json({
                "type": "webrtc_track_state",
                "camera": True,
                "screen": False
            })
            state_rcv2 = ws2.receive_json()
            assert state_rcv2["type"] == "webrtc_track_state"
            assert state_rcv2["camera"] is True
            assert state_rcv2["screen"] is False
