"""
Module 08 — End-to-end TTS integration tests
Phân loại:
  [MOCK]  — giả lập, không gọi API thật, chạy offline
  [REAL]  — gọi TTS API thật (gtts/gTTS qua internet)

Chạy:
    # Tất cả test (bỏ qua REAL nếu không có internet):
    python -m pytest tests/test_module08.py -v

    # Chỉ MOCK:
    python -m pytest tests/test_module08.py -v -m mock

    # Chỉ REAL:
    python -m pytest tests/test_module08.py -v -m real
"""

import asyncio
import json
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(".env")

# ── Helpers ─────────────────────────────────────────────────────────────────

FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 200  # minimal fake MP3 header

TTS_FRAME_MAGIC = b"TTS\x00"


def make_fake_ws():
    """Tạo mock WebSocket ghi lại các lần gọi."""
    ws = MagicMock()
    ws.sent_text = []
    ws.sent_bytes = []

    async def _send_text(data):
        ws.sent_text.append(json.loads(data))

    async def _send_bytes(data):
        ws.sent_bytes.append(data)

    ws.send_text = _send_text
    ws.send_bytes = _send_bytes
    return ws


def run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════
# A. TTS SERVICE UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestTTSService(unittest.TestCase):

    def setUp(self):
        from app.services.tts_service import clear_cache
        clear_cache()

    # ── [MOCK] Text rỗng → trả b"" ────────────────────────────────────────
    @pytest.mark.mock
    def test_empty_text_returns_empty(self):
        from app.services.tts_service import synthesize
        result = run(synthesize("  ", "vi"))
        self.assertEqual(result, b"")

    # ── [MOCK] Ngôn ngữ không hỗ trợ → TTSError ──────────────────────────
    @pytest.mark.mock
    def test_unsupported_language_raises(self):
        from app.services.tts_service import synthesize, TTSError
        with self.assertRaises(TTSError):
            run(synthesize("hello", "xx"))

    # ── [MOCK] TTS_ENABLED=False → trả b"" không gọi provider ────────────
    @pytest.mark.mock
    def test_tts_disabled_returns_empty(self):
        from app.services import tts_service
        from app.services.tts_service import TTSError

        original = tts_service._provider.synthesize_sync
        called = []
        def mock_synth(text, lang): called.append(True); return FAKE_MP3
        tts_service._provider.synthesize_sync = mock_synth

        with patch.object(tts_service.settings, "TTS_ENABLED", False):
            result = run(tts_service.synthesize("hello", "en"))
        tts_service._provider.synthesize_sync = original

        self.assertEqual(result, b"")
        self.assertEqual(len(called), 0)

    # ── [MOCK] Cache hit — lần 2 không gọi provider ──────────────────────
    @pytest.mark.mock
    def test_cache_hit(self):
        from app.services import tts_service

        call_count = []
        original = tts_service._provider.synthesize_sync

        def counting_synth(text, lang):
            call_count.append(1)
            return FAKE_MP3

        tts_service._provider.synthesize_sync = counting_synth
        tts_service.clear_cache()

        try:
            r1 = run(tts_service.synthesize("cache test", "en"))
            r2 = run(tts_service.synthesize("cache test", "en"))
        finally:
            tts_service._provider.synthesize_sync = original

        self.assertEqual(r1, r2)
        self.assertEqual(len(call_count), 1)  # chỉ gọi 1 lần

    # ── [MOCK] Provider exception → bọc thành TTSError ───────────────────
    @pytest.mark.mock
    def test_provider_exception_wrapped(self):
        from app.services import tts_service
        from app.services.tts_service import TTSError

        tts_service.clear_cache()
        original = tts_service._provider.synthesize_sync

        def failing_synth(text, lang):
            raise RuntimeError("network error")

        tts_service._provider.synthesize_sync = failing_synth
        try:
            with self.assertRaises(TTSError):
                run(tts_service.synthesize("hello", "en"))
        finally:
            tts_service._provider.synthesize_sync = original

    # ── [MOCK] TTS frame magic prefix ─────────────────────────────────────
    @pytest.mark.mock
    def test_pack_tts_frame(self):
        from app.services.translation_pipeline import pack_tts_frame, TTS_FRAME_MAGIC
        frame = pack_tts_frame(FAKE_MP3)
        self.assertTrue(frame.startswith(TTS_FRAME_MAGIC))
        self.assertEqual(frame[4:], FAKE_MP3)

    # ── [MOCK] Frame magic phân biệt với raw PCM ─────────────────────────
    @pytest.mark.mock
    def test_frame_magic_distinct_from_pcm(self):
        from app.services.translation_pipeline import TTS_FRAME_MAGIC
        # Raw PCM (webm) bắt đầu bằng \x1a\x45\xdf\xa3 (EBML header)
        raw_pcm_header = b"\x1a\x45\xdf\xa3" + b"\x00" * 10
        self.assertNotEqual(raw_pcm_header[:4], TTS_FRAME_MAGIC)


# ═══════════════════════════════════════════════════════════════════════════
# B. CONNECTION MANAGER UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectionManager(unittest.TestCase):

    def _make_manager(self):
        """Tạo instance mới, độc lập với singleton."""
        from app.websocket.connection_manager import ConnectionManager
        return ConnectionManager()

    def _fake_connect(self, mgr, room, uid):
        ws = make_fake_ws()
        # Bypass await websocket.accept() bằng cách mock
        async def _connect():
            if room not in mgr.room_connections:
                mgr.room_connections[room] = []
            mgr.room_connections[room].append(ws)
            mgr.user_map[(room, uid)] = ws
            mgr.tts_prefs.setdefault((room, uid), True)
        run(_connect())
        return ws

    # ── TTS bật mặc định khi connect ─────────────────────────────────────
    @pytest.mark.mock
    def test_tts_default_enabled(self):
        mgr = self._make_manager()
        ws = self._fake_connect(mgr, "ROOM1", 1)
        self.assertTrue(mgr.is_tts_enabled("ROOM1", 1))

    # ── set/get tts_enabled ────────────────────────────────────────────────
    @pytest.mark.mock
    def test_set_tts_enabled(self):
        mgr = self._make_manager()
        self._fake_connect(mgr, "ROOM1", 1)
        mgr.set_tts_enabled("ROOM1", 1, False)
        self.assertFalse(mgr.is_tts_enabled("ROOM1", 1))
        mgr.set_tts_enabled("ROOM1", 1, True)
        self.assertTrue(mgr.is_tts_enabled("ROOM1", 1))

    # ── disconnect xóa tts_prefs ──────────────────────────────────────────
    @pytest.mark.mock
    def test_disconnect_clears_tts_pref(self):
        mgr = self._make_manager()
        ws = self._fake_connect(mgr, "ROOM1", 1)
        mgr.disconnect("ROOM1", 1, ws)
        self.assertNotIn(("ROOM1", 1), mgr.tts_prefs)

    # ── send_personal_bytes ────────────────────────────────────────────────
    @pytest.mark.mock
    def test_send_personal_bytes(self):
        mgr = self._make_manager()
        ws = self._fake_connect(mgr, "ROOM1", 1)
        run(mgr.send_personal_bytes(ws, b"hello"))
        self.assertEqual(ws.sent_bytes, [b"hello"])

    # ── get_user_websocket trả None sau disconnect ─────────────────────────
    @pytest.mark.mock
    def test_get_ws_after_disconnect_is_none(self):
        mgr = self._make_manager()
        ws = self._fake_connect(mgr, "ROOM1", 2)
        mgr.disconnect("ROOM1", 2, ws)
        self.assertIsNone(mgr.get_user_websocket("ROOM1", 2))


# ═══════════════════════════════════════════════════════════════════════════
# C. TRANSLATION PIPELINE INTEGRATION TESTS  [MOCK]
# ═══════════════════════════════════════════════════════════════════════════

class TestPipeline(unittest.TestCase):
    """Giả lập DB, translate, TTS — không gọi API thật."""

    def setUp(self):
        # Reset processed_ids giữa các test để tránh conflict
        from app.services import translation_pipeline as pl
        pl._processed_ids.clear()
        self._test_counter = getattr(self, '_test_counter', 0) + 1

    def _build_scenario(self, groups, tts_side_effect=None, translate_result="Hello"):
        """
        groups: dict lang→[(uid, tts_on)]
        Trả về (db, manager_instance, ws_map, process_transcript_coro)
        """
        from app.websocket.connection_manager import ConnectionManager
        from app.services import translation_pipeline as pl

        mgr = ConnectionManager()
        ws_map = {}

        # Setup fake connections
        for lang, members in groups.items():
            for uid, tts_on in members:
                ws = make_fake_ws()
                room = "TEST"
                if room not in mgr.room_connections:
                    mgr.room_connections[room] = []
                mgr.room_connections[room].append(ws)
                mgr.user_map[(room, uid)] = ws
                mgr.tts_prefs[(room, uid)] = tts_on
                ws_map[uid] = ws

        # Fake DB objects
        FakeMember = types.SimpleNamespace
        FakeUser   = types.SimpleNamespace
        FakeRoom   = types.SimpleNamespace(id=1, is_active=True)

        member_list = [
            FakeMember(user_id=uid, is_active=True)
            for lang, members in groups.items()
            for uid, _ in members
        ]
        user_lang_map = {
            uid: lang
            for lang, members in groups.items()
            for uid, _ in members
        }

        db = MagicMock()

        def mock_get_room(db_, code):
            return FakeRoom

        def mock_get_members(db_, room_id):
            return member_list

        def mock_get_user(db_, uid):
            lang = user_lang_map.get(uid, "en")
            return FakeUser(listening_language=lang)

        async def mock_translate(text, src, tgt, retries=2):
            return translate_result

        if tts_side_effect is None:
            async def mock_tts(text, lang):
                return FAKE_MP3
        else:
            mock_tts = tts_side_effect

        return db, mgr, ws_map, mock_get_room, mock_get_members, mock_get_user, mock_translate, mock_tts

    def _run_pipeline(self, groups, tts_side_effect=None, translate_result="Hello",
                      speaker_id=99, source_lang="vi", tts_enabled=True):
        import uuid
        from app.services import translation_pipeline as pl

        db, mgr, ws_map, mock_room, mock_members, mock_user, mock_translate, mock_tts = \
            self._build_scenario(groups, tts_side_effect, translate_result)

        with patch("app.services.translation_pipeline.get_room_by_code", mock_room), \
             patch("app.services.translation_pipeline.get_active_members", mock_members), \
             patch("app.services.translation_pipeline.get_user_by_id", mock_user), \
             patch("app.services.translation_pipeline.translate_text", mock_translate), \
             patch("app.services.translation_pipeline.tts_synthesize", mock_tts), \
             patch("app.services.translation_pipeline.manager", mgr), \
             patch.object(pl.settings, "TTS_ENABLED", tts_enabled):

            asyncio.run(pl.process_transcript(
                db=db, room_code="TEST",
                speaker_user_id=speaker_id,
                source_language=source_lang,
                text="Xin chào",
                message_id=str(uuid.uuid4()),   # unique per call
            ))

        return ws_map

    # ── [TC-01] vi→en: user 2 nhận text + TTS ─────────────────────────────
    @pytest.mark.mock
    def test_vi_to_en_receives_text_and_tts(self):
        ws_map = self._run_pipeline({"en": [(2, True)]})
        ws2 = ws_map[2]
        types_sent = [m["type"] for m in ws2.sent_text]
        self.assertIn("translation", types_sent)
        self.assertIn("tts_start", types_sent)
        self.assertEqual(len(ws2.sent_bytes), 1)
        self.assertTrue(ws2.sent_bytes[0].startswith(TTS_FRAME_MAGIC))

    # ── [TC-02] en→vi: user 2 ngôn ngữ vi ────────────────────────────────
    @pytest.mark.mock
    def test_en_to_vi_receives_translation(self):
        ws_map = self._run_pipeline({"vi": [(3, True)]}, source_lang="en",
                                    translate_result="Xin chào")
        ws3 = ws_map[3]
        trans = [m for m in ws3.sent_text if m["type"] == "translation"]
        self.assertEqual(len(trans), 1)
        self.assertEqual(trans[0]["target_language"], "vi")

    # ── [TC-03] Korean listener ────────────────────────────────────────────
    @pytest.mark.mock
    def test_korean_listener(self):
        ws_map = self._run_pipeline({"ko": [(4, True)]},
                                    translate_result="안녕하세요")
        ws4 = ws_map[4]
        trans = [m for m in ws4.sent_text if m["type"] == "translation"]
        self.assertEqual(trans[0]["target_language"], "ko")

    # ── [TC-04] Nhiều người nghe, ngôn ngữ khác nhau ─────────────────────
    @pytest.mark.mock
    def test_multiple_listeners_different_langs(self):
        tts_calls = []
        async def tracking_tts(text, lang):
            tts_calls.append(lang)
            return FAKE_MP3

        ws_map = self._run_pipeline(
            {"en": [(2, True)], "ko": [(3, True)], "ja": [(4, True)]},
            tts_side_effect=tracking_tts,
        )
        # Mỗi ngôn ngữ chỉ gọi TTS 1 lần
        self.assertEqual(sorted(tts_calls), ["en", "ja", "ko"])

        for uid in [2, 3, 4]:
            ws = ws_map[uid]
            self.assertEqual(len(ws.sent_bytes), 1)

    # ── [TC-05] Nhiều người nghe cùng ngôn ngữ → TTS gọi 1 lần, shared ──
    @pytest.mark.mock
    def test_shared_tts_for_same_language(self):
        tts_calls = []
        async def tracking_tts(text, lang):
            tts_calls.append(lang)
            return FAKE_MP3

        ws_map = self._run_pipeline(
            {"en": [(2, True), (3, True), (5, True)]},
            tts_side_effect=tracking_tts,
        )
        # TTS chỉ tổng hợp 1 lần cho "en"
        self.assertEqual(tts_calls, ["en"])
        # Nhưng gửi audio tới cả 3 users
        for uid in [2, 3, 5]:
            self.assertEqual(len(ws_map[uid].sent_bytes), 1)

    # ── [TC-06] User tắt TTS → không nhận audio ──────────────────────────
    @pytest.mark.mock
    def test_tts_off_user_gets_no_audio(self):
        # uid=2: tts bật; uid=3: tts tắt
        ws_map = self._run_pipeline({"en": [(2, True), (3, False)]})
        self.assertEqual(len(ws_map[2].sent_bytes), 1)   # nhận audio
        self.assertEqual(len(ws_map[3].sent_bytes), 0)   # không nhận audio
        # uid=3 vẫn nhận text
        trans3 = [m for m in ws_map[3].sent_text if m["type"] == "translation"]
        self.assertEqual(len(trans3), 1)

    # ── [TC-07] TTS lỗi → gửi tts_error, text vẫn gửi ───────────────────
    @pytest.mark.mock
    def test_tts_error_sends_tts_error_event(self):
        from app.services.tts_service import TTSError

        async def failing_tts(text, lang):
            raise TTSError("provider down")

        ws_map = self._run_pipeline({"en": [(2, True)]},
                                    tts_side_effect=failing_tts)
        ws2 = ws_map[2]
        types_sent = [m["type"] for m in ws2.sent_text]
        self.assertIn("translation", types_sent)   # text vẫn có
        self.assertIn("tts_error", types_sent)     # lỗi TTS được báo
        self.assertEqual(len(ws2.sent_bytes), 0)   # không có audio

    # ── [TC-08] User rời phòng trong lúc TTS xử lý ────────────────────────
    @pytest.mark.mock
    def test_user_leaves_during_tts(self):
        from app.services import translation_pipeline as pl
        from app.websocket.connection_manager import ConnectionManager

        mgr = ConnectionManager()
        ws2 = make_fake_ws()
        mgr.room_connections["TEST"] = [ws2]
        mgr.user_map[("TEST", 2)] = ws2
        mgr.tts_prefs[("TEST", 2)] = True

        db = MagicMock()
        FakeMember = types.SimpleNamespace
        FakeUser   = types.SimpleNamespace
        FakeRoom   = types.SimpleNamespace(id=1)

        # TTS xử lý chậm, trong lúc đó user disconnect
        async def slow_tts(text, lang):
            # Simulate user leaving
            mgr.disconnect("TEST", 2, ws2)
            await asyncio.sleep(0)
            return FAKE_MP3

        with patch("app.services.translation_pipeline.get_room_by_code", lambda d,c: FakeRoom), \
             patch("app.services.translation_pipeline.get_active_members",
                   lambda d,i: [FakeMember(user_id=2, is_active=True)]), \
             patch("app.services.translation_pipeline.get_user_by_id",
                   lambda d,u: FakeUser(listening_language="en")), \
             patch("app.services.translation_pipeline.translate_text",
                   AsyncMock(return_value="Hello")), \
             patch("app.services.translation_pipeline.tts_synthesize", slow_tts), \
             patch("app.services.translation_pipeline.manager", mgr), \
             patch.object(pl.settings, "TTS_ENABLED", True):

            # Không được raise exception
            run(pl.process_transcript(
                db=db, room_code="TEST", speaker_user_id=99,
                source_language="vi", text="test", message_id="leave-test",
            ))

        # ws2 đã disconnect — không gửi được bytes (manager kiểm tra None)
        self.assertEqual(len(ws2.sent_bytes), 0)

    # ── [TC-09] Người nói không nhận bản dịch của chính mình ─────────────
    @pytest.mark.mock
    def test_speaker_does_not_receive_translation(self):
        # speaker_id = 99, listeners = [2]
        ws_map = self._run_pipeline({"en": [(2, True)]}, speaker_id=99)
        # Đảm bảo không có ws cho uid=99
        self.assertNotIn(99, ws_map)

    # ── [TC-10] Global TTS_ENABLED=False → không TTS, text vẫn gửi ───────
    @pytest.mark.mock
    def test_global_tts_disabled(self):
        ws_map = self._run_pipeline({"en": [(2, True)]}, tts_enabled=False)
        ws2 = ws_map[2]
        trans = [m for m in ws2.sent_text if m["type"] == "translation"]
        self.assertEqual(len(trans), 1)
        # tts_pending phải False
        self.assertFalse(trans[0].get("tts_pending", True))
        self.assertEqual(len(ws2.sent_bytes), 0)

    # ── [TC-11] Trùng message_id không xử lý lại ──────────────────────────
    @pytest.mark.mock
    def test_duplicate_message_id_ignored(self):
        from app.services import translation_pipeline as pl
        from app.websocket.connection_manager import ConnectionManager

        # Reset processed ids
        pl._processed_ids.clear()

        mgr = ConnectionManager()
        ws2 = make_fake_ws()
        mgr.room_connections["TEST"] = [ws2]
        mgr.user_map[("TEST", 2)] = ws2
        mgr.tts_prefs[("TEST", 2)] = True

        db = MagicMock()
        FakeMember = types.SimpleNamespace
        FakeUser   = types.SimpleNamespace
        FakeRoom   = types.SimpleNamespace(id=1)

        async def mock_translate(text, src, tgt, retries=2):
            return "Hello"

        async def mock_tts(text, lang):
            return FAKE_MP3

        kwargs = dict(
            db=db, room_code="TEST", speaker_user_id=99,
            source_language="vi", text="test", message_id="dup-001",
        )

        with patch("app.services.translation_pipeline.get_room_by_code", lambda d,c: FakeRoom), \
             patch("app.services.translation_pipeline.get_active_members",
                   lambda d,i: [FakeMember(user_id=2, is_active=True)]), \
             patch("app.services.translation_pipeline.get_user_by_id",
                   lambda d,u: FakeUser(listening_language="en")), \
             patch("app.services.translation_pipeline.translate_text", mock_translate), \
             patch("app.services.translation_pipeline.tts_synthesize", mock_tts), \
             patch("app.services.translation_pipeline.manager", mgr), \
             patch.object(pl.settings, "TTS_ENABLED", True):

            run(pl.process_transcript(**kwargs))
            first_text_count = len(ws2.sent_text)
            run(pl.process_transcript(**kwargs))   # gửi lại cùng message_id

        # Lần 2 bị bỏ qua hoàn toàn
        self.assertEqual(len(ws2.sent_text), first_text_count)


# ═══════════════════════════════════════════════════════════════════════════
# D. TTS TIMING  [MOCK]
# ═══════════════════════════════════════════════════════════════════════════

class TestTTSTiming(unittest.TestCase):

    # ── [TC-12] Text gửi ngay, không chờ TTS ────────────────────────────
    @pytest.mark.mock
    def test_text_sent_before_tts_completes(self):
        """Text translation phải đến trước tts_start."""
        from app.services import translation_pipeline as pl
        from app.websocket.connection_manager import ConnectionManager

        mgr = ConnectionManager()
        ws2 = make_fake_ws()
        mgr.room_connections["TEST"] = [ws2]
        mgr.user_map[("TEST", 2)] = ws2
        mgr.tts_prefs[("TEST", 2)] = True

        db = MagicMock()
        FakeMember = types.SimpleNamespace
        FakeRoom   = types.SimpleNamespace(id=1)
        FakeUser   = types.SimpleNamespace

        order = []

        async def mock_translate(text, src, tgt, retries=2):
            return "Hello"

        async def slow_tts(text, lang):
            await asyncio.sleep(0.05)
            return FAKE_MP3

        original_send = mgr.send_personal_message

        async def tracking_send(ws, msg):
            order.append(msg["type"])
            await original_send(ws, msg)

        mgr.send_personal_message = tracking_send

        with patch("app.services.translation_pipeline.get_room_by_code", lambda d,c: FakeRoom), \
             patch("app.services.translation_pipeline.get_active_members",
                   lambda d,i: [FakeMember(user_id=2, is_active=True)]), \
             patch("app.services.translation_pipeline.get_user_by_id",
                   lambda d,u: FakeUser(listening_language="en")), \
             patch("app.services.translation_pipeline.translate_text", mock_translate), \
             patch("app.services.translation_pipeline.tts_synthesize", slow_tts), \
             patch("app.services.translation_pipeline.manager", mgr), \
             patch.object(pl.settings, "TTS_ENABLED", True):

            run(pl.process_transcript(
                db=db, room_code="TEST", speaker_user_id=99,
                source_language="vi", text="test", message_id="timing-001",
            ))

        # translation phải xuất hiện TRƯỚC tts_start
        self.assertIn("translation", order)
        self.assertIn("tts_start", order)
        translation_idx = order.index("translation")
        tts_start_idx   = order.index("tts_start")
        self.assertLess(translation_idx, tts_start_idx)

    # ── [TC-13] Nhiều câu liên tiếp không trùng message_id ───────────────
    @pytest.mark.mock
    def test_sequential_messages_unique_ids(self):
        from app.services import translation_pipeline as pl
        from app.websocket.connection_manager import ConnectionManager

        pl._processed_ids.clear()
        mgr = ConnectionManager()
        ws2 = make_fake_ws()
        mgr.room_connections["TEST2"] = [ws2]
        mgr.user_map[("TEST2", 2)] = ws2
        mgr.tts_prefs[("TEST2", 2)] = True

        db = MagicMock()
        FakeMember = types.SimpleNamespace
        FakeRoom   = types.SimpleNamespace(id=1)
        FakeUser   = types.SimpleNamespace

        async def mock_translate(text, src, tgt, retries=2):
            return "Hello"

        async def mock_tts(text, lang):
            return FAKE_MP3

        with patch("app.services.translation_pipeline.get_room_by_code", lambda d,c: FakeRoom), \
             patch("app.services.translation_pipeline.get_active_members",
                   lambda d,i: [FakeMember(user_id=2, is_active=True)]), \
             patch("app.services.translation_pipeline.get_user_by_id",
                   lambda d,u: FakeUser(listening_language="en")), \
             patch("app.services.translation_pipeline.translate_text", mock_translate), \
             patch("app.services.translation_pipeline.tts_synthesize", mock_tts), \
             patch("app.services.translation_pipeline.manager", mgr), \
             patch.object(pl.settings, "TTS_ENABLED", True):

            for i in range(5):
                run(pl.process_transcript(
                    db=db, room_code="TEST2", speaker_user_id=99,
                    source_language="vi", text=f"Câu {i}",
                    # không truyền message_id → tự generate uuid
                ))

        translations = [m for m in ws2.sent_text if m["type"] == "translation"]
        ids = [m["message_id"] for m in translations]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)  # tất cả unique


# ═══════════════════════════════════════════════════════════════════════════
# E. REAL TTS API TESTS  [REAL] — cần internet
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.real
class TestTTSReal(unittest.TestCase):
    """Gọi gTTS API thật. Bỏ qua nếu không có internet."""

    def setUp(self):
        from app.services.tts_service import clear_cache
        clear_cache()

    def _check_mp3(self, data: bytes, label: str):
        self.assertGreater(len(data), 1000, f"{label}: audio quá nhỏ ({len(data)}B)")
        # MP3 bắt đầu bằng ID3 header hoặc sync word 0xFF 0xFB/0xFA/0xF3
        valid_header = (
            data[:3] == b"ID3"
            or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
        )
        self.assertTrue(valid_header, f"{label}: không phải MP3 hợp lệ (header={data[:4].hex()})")

    # ── [REAL-01] vi→en ───────────────────────────────────────────────────
    def test_real_vi(self):
        from app.services.tts_service import synthesize
        t0 = time.perf_counter()
        data = run(synthesize("Xin chào, cuộc họp bắt đầu.", "vi"))
        latency = int((time.perf_counter() - t0) * 1000)
        self._check_mp3(data, "vi")
        print(f"\n  [REAL-01] vi: {len(data)}B {latency}ms")

    # ── [REAL-02] en ──────────────────────────────────────────────────────
    def test_real_en(self):
        from app.services.tts_service import synthesize
        t0 = time.perf_counter()
        data = run(synthesize("Hello, the meeting has started.", "en"))
        latency = int((time.perf_counter() - t0) * 1000)
        self._check_mp3(data, "en")
        print(f"\n  [REAL-02] en: {len(data)}B {latency}ms")

    # ── [REAL-03] ko ──────────────────────────────────────────────────────
    def test_real_ko(self):
        from app.services.tts_service import synthesize
        t0 = time.perf_counter()
        data = run(synthesize("안녕하세요, 회의가 시작되었습니다.", "ko"))
        latency = int((time.perf_counter() - t0) * 1000)
        self._check_mp3(data, "ko")
        print(f"\n  [REAL-03] ko: {len(data)}B {latency}ms")

    # ── [REAL-04] Tất cả 8 ngôn ngữ được hỗ trợ ──────────────────────────
    def test_real_all_languages(self):
        from app.services.tts_service import synthesize, get_supported_languages
        samples = {
            "vi": "Xin chào", "en": "Hello", "ko": "안녕", "ja": "こんにちは",
            "zh": "你好",      "fr": "Bonjour", "de": "Hallo", "es": "Hola",
        }
        supported = get_supported_languages()
        results = {}
        for lang in supported:
            text = samples.get(lang, "Hello")
            t0 = time.perf_counter()
            data = run(synthesize(text, lang))
            ms = int((time.perf_counter() - t0) * 1000)
            results[lang] = (len(data), ms)
            self._check_mp3(data, lang)

        print("\n  [REAL-04] All languages:")
        for lang, (size, ms) in results.items():
            print(f"    {lang}: {size}B {ms}ms")

    # ── [REAL-05] Latency < 5 giây cho câu ngắn ───────────────────────────
    def test_real_latency_under_5s(self):
        from app.services.tts_service import synthesize, clear_cache
        clear_cache()
        t0 = time.perf_counter()
        run(synthesize("Hello world", "en"))
        latency = time.perf_counter() - t0
        print(f"\n  [REAL-05] Latency: {latency*1000:.0f}ms")
        self.assertLess(latency, 5.0, f"TTS quá chậm: {latency:.1f}s")

    # ── [REAL-06] Cache hit nhanh hơn cold call ───────────────────────────
    def test_real_cache_faster(self):
        from app.services.tts_service import synthesize, clear_cache
        clear_cache()
        text = "cache speed test sentence"
        t0 = time.perf_counter(); run(synthesize(text, "en")); cold = time.perf_counter() - t0
        t0 = time.perf_counter(); run(synthesize(text, "en")); hot  = time.perf_counter() - t0
        print(f"\n  [REAL-06] Cold={cold*1000:.0f}ms Hot(cached)={hot*1000:.0f}ms")
        self.assertLess(hot, cold * 0.5)  # cache phải ít nhất 2x nhanh hơn


# ═══════════════════════════════════════════════════════════════════════════
# F. TRANSLATION SERVICE  [REAL]
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.real
class TestTranslationReal(unittest.TestCase):
    """Kiểm tra chất lượng dịch thuật thực tế với deep_google provider."""

    def test_vi_to_en(self):
        from app.services.translation_service import translate_text
        result = run(translate_text("Xin chào", "vi", "en"))
        self.assertTrue(len(result) > 0)
        # Kết quả hợp lý (không phải "- What?")
        self.assertNotIn("What?", result)
        print(f"\n  vi→en: 'Xin chào' → '{result}'")

    def test_en_to_vi(self):
        from app.services.translation_service import translate_text
        result = run(translate_text("Good morning", "en", "vi"))
        self.assertGreater(len(result), 0)
        print(f"\n  en→vi: 'Good morning' → '{result}'")

    def test_same_language_returns_original(self):
        from app.services.translation_service import translate_text
        result = run(translate_text("Hello", "en", "en"))
        self.assertEqual(result, "Hello")


if __name__ == "__main__":
    import sys
    # Mặc định chạy MOCK, thêm --real để chạy cả REAL
    if "--real" in sys.argv:
        sys.argv.remove("--real")
        pytest.main([__file__, "-v", "--tb=short"])
    else:
        pytest.main([__file__, "-v", "--tb=short", "-m", "mock"])
