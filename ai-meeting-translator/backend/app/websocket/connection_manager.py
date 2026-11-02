from fastapi import WebSocket
from starlette.websockets import WebSocketState, WebSocketDisconnect
import json
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Quản lý WebSocket connection theo room.
    room_connections: { room_code: [ws1, ws2, ...] }
    user_map:         { (room_code, user_id): ws }
    tts_prefs:        { (room_code, user_id): bool }
    """

    def __init__(self):
        self.room_connections: dict[str, list[WebSocket]] = {}
        self.user_map: dict[tuple, WebSocket] = {}
        self.tts_prefs: dict[tuple, bool] = {}

    async def connect(self, room_code: str, user_id: int, websocket: WebSocket):
        """
        Đăng ký websocket mới cho (room_code, user_id).
        Nếu user đã có kết nối cũ trước đó trong phòng, đóng kết nối cũ an toàn.
        """
        old_ws = self.user_map.get((room_code, user_id))
        if old_ws and old_ws != websocket:
            try:
                await old_ws.close(code=1000, reason="Replaced by new connection")
            except Exception:
                pass
            if room_code in self.room_connections and old_ws in self.room_connections[room_code]:
                self.room_connections[room_code] = [w for w in self.room_connections[room_code] if w != old_ws]

        if getattr(websocket, "client_state", None) != WebSocketState.CONNECTED:
            await websocket.accept()

        if room_code not in self.room_connections:
            self.room_connections[room_code] = []
        if websocket not in self.room_connections[room_code]:
            self.room_connections[room_code].append(websocket)
        self.user_map[(room_code, user_id)] = websocket
        self.tts_prefs.setdefault((room_code, user_id), True)

        try:
            from app.services.ai_monitor_service import ai_monitor
            ai_monitor.record_ws_connected()
        except Exception:
            pass

    def disconnect(self, room_code: str, user_id: int, websocket: WebSocket = None):
        """
        Dọn dẹp kết nối an toàn.
        Nếu websocket được truyền vào, chỉ gỡ nếu đúng websocket đó.
        """
        if room_code in self.room_connections:
            if websocket is not None:
                self.room_connections[room_code] = [
                    ws for ws in self.room_connections[room_code] if ws != websocket
                ]
            else:
                target_ws = self.user_map.get((room_code, user_id))
                if target_ws:
                    self.room_connections[room_code] = [
                        ws for ws in self.room_connections[room_code] if ws != target_ws
                    ]
            if not self.room_connections[room_code]:
                self.room_connections.pop(room_code, None)

        current_ws = self.user_map.get((room_code, user_id))
        if websocket is None or current_ws == websocket:
            self.user_map.pop((room_code, user_id), None)
            self.tts_prefs.pop((room_code, user_id), None)

    def get_user_websocket(self, room_code: str, user_id: int) -> WebSocket | None:
        return self.user_map.get((room_code, user_id))

    def set_tts_enabled(self, room_code: str, user_id: int, enabled: bool) -> None:
        self.tts_prefs[(room_code, user_id)] = enabled

    def is_tts_enabled(self, room_code: str, user_id: int) -> bool:
        return self.tts_prefs.get((room_code, user_id), True)

    async def send_personal_message(self, websocket: WebSocket, message: dict) -> bool:
        """
        Gửi JSON message an toàn.
        Trả về True nếu gửi thành công, False nếu kết nối đã đóng hoặc lỗi.
        Không để văng ngoại lệ làm sập caller.
        """
        if not websocket or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED:
            return False
        try:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except (WebSocketDisconnect, RuntimeError, Exception) as e:
            logger.debug(f"[WS Manager] send_personal_message failed: {e}")
            return False

    async def send_personal_bytes(self, websocket: WebSocket, data: bytes) -> bool:
        """
        Gửi raw bytes an toàn.
        Trả về True nếu gửi thành công, False nếu kết nối đã đóng hoặc lỗi.
        """
        if not websocket or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED:
            return False
        try:
            await websocket.send_bytes(data)
            return True
        except (WebSocketDisconnect, RuntimeError, Exception) as e:
            logger.debug(f"[WS Manager] send_personal_bytes failed: {e}")
            return False

    async def broadcast(self, room_code: str, message: dict, exclude: WebSocket = None):
        """
        Broadcast JSON message tới các client trong room.
        Tự động phát hiện và dọn dẹp các socket đã chết an toàn.
        """
        if room_code not in self.room_connections:
            return
        connections = list(self.room_connections.get(room_code, []))
        payload = json.dumps(message, ensure_ascii=False)
        dead = []
        for ws in connections:
            if ws == exclude:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            key = next((k for k, v in list(self.user_map.items()) if v == ws and k[0] == room_code), None)
            if key:
                self.disconnect(room_code, key[1], ws)
            else:
                if room_code in self.room_connections and ws in self.room_connections[room_code]:
                    self.room_connections[room_code].remove(ws)

    async def broadcast_binary(self, room_code: str, data: bytes, exclude: WebSocket = None):
        """Broadcast binary data tới các client trong room."""
        if room_code not in self.room_connections:
            return
        connections = list(self.room_connections.get(room_code, []))
        dead = []
        for ws in connections:
            if ws == exclude:
                continue
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            key = next((k for k, v in list(self.user_map.items()) if v == ws and k[0] == room_code), None)
            if key:
                self.disconnect(room_code, key[1], ws)
            else:
                if room_code in self.room_connections and ws in self.room_connections[room_code]:
                    self.room_connections[room_code].remove(ws)

    def get_room_connection_count(self, room_code: str) -> int:
        return len(self.room_connections.get(room_code, []))

    def get_room_users(self, room_code: str, exclude: int = None) -> list[int]:
        """Trả danh sách user_id đang online trong room, bỏ qua exclude."""
        return [
            uid for (rc, uid), _ in list(self.user_map.items())
            if rc == room_code and uid != exclude
        ]


manager = ConnectionManager()
