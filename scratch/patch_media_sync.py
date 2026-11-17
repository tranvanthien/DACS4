# -*- coding: utf-8 -*-
import os

base_dir = r"d:\HK1 26-27\DACS4\ai-meeting-translator\frontend"

# 4. Patch room.html
room_path = os.path.join(base_dir, "user", "room.html")
with open(room_path, "r", encoding="utf-8") as f:
    room_code = f.read()

old_boot_user = """      userName = p.get('userName') || user.full_name || 'User';

      if (!roomCode) { location.href = 'home.html'; return; }"""

new_boot_user = """      userName = p.get('userName') || user.full_name || 'User';

      // Đọc trạng thái khởi tạo cam và mic từ URL params hoặc sessionStorage
      const pMic = p.get('mic');
      const pCam = p.get('cam');
      if (pMic !== null) {
        micOn = (pMic === '1' || pMic === 'true');
      } else {
        const sMic = sessionStorage.getItem('linguameet_pref_mic');
        micOn = (sMic === null || sMic === '1' || sMic === 'true');
      }
      if (pCam !== null) {
        camOn = (pCam === '1' || pCam === 'true');
      } else {
        const sCam = sessionStorage.getItem('linguameet_pref_cam');
        camOn = (sCam === '1' || sCam === 'true');
      }

      if (!roomCode) { location.href = 'home.html'; return; }"""

old_boot_tile = """      // Render own tile
      renderSelfTile();
      addMember(user.id, userName, currentSpeak, true);"""

new_boot_tile = """      // Render own tile
      renderSelfTile();
      addMember(user.id, userName, currentSpeak, true);
      updateMediaButtonsUI();"""

old_join_call_room = """    async function joinCall() {
      if (!ws || ws.readyState !== WebSocket.OPEN) { T.err('Chưa kết nối', 'Đang chờ kết nối máy chủ...'); return; }
      WebRTCService.setWebSocket(ws);
      try {
        const peersToOffer = roomPeers.filter(id => id !== user.id);
        await WebRTCService.joinCall(user.id, peersToOffer);
      } catch (e) { T.err('Không vào được cuộc gọi', e.message); }
    }"""

new_join_call_room = """    function updateMediaButtonsUI() {
      const micBtn = document.getElementById('micBtn');
      const micLabel = document.getElementById('micLabel');
      const selfMicIcon = document.getElementById('selfMicIcon');
      if (micBtn) {
        micBtn.classList.toggle('off', !micOn);
        micBtn.textContent = micOn ? '🎤' : '🔇';
      }
      if (micLabel) {
        micLabel.textContent = micOn ? 'Mic' : 'Tắt mic';
      }
      if (selfMicIcon) {
        selfMicIcon.textContent = micOn ? '🎤' : '🔇';
        selfMicIcon.classList.toggle('muted', !micOn);
      }

      const camBtn = document.getElementById('camBtn');
      const camLabel = document.getElementById('camLabel');
      if (camBtn) {
        camBtn.classList.toggle('active', !!camOn);
        camBtn.textContent = camOn ? '📹' : '📷';
      }
      if (camLabel) {
        camLabel.textContent = camOn ? 'Tắt cam' : 'Camera';
      }
    }

    async function joinCall() {
      if (!ws || ws.readyState !== WebSocket.OPEN) { T.err('Chưa kết nối', 'Đang chờ kết nối máy chủ...'); return; }
      WebRTCService.setWebSocket(ws);
      try {
        const peersToOffer = roomPeers.filter(id => id !== user.id);
        await WebRTCService.joinCall(user.id, peersToOffer, {
          initialMic: micOn,
          initialCam: camOn
        });
      } catch (e) { T.err('Không vào được cuộc gọi', e.message); }
    }"""

old_rtc_event = """    function onRTCEvent(ev) {
      switch (ev.type) {
        case 'call_joined':
          document.getElementById('wrapJoin').style.display = 'none';
          document.getElementById('wrapMic').style.display = 'flex';
          document.getElementById('wrapCam').style.display = 'flex';
          document.getElementById('wrapScreen').style.display = 'flex';
          startTimer();
          micOn = true;
          if (WebRTCService.localStream) {
            const t = WebRTCService.localStream.getAudioTracks()[0];
            if (t?.readyState === 'live') {
              if (roomMode === 'ai') {
                AutoSTT.start(WebRTCService.localStream, ws, currentSpeak);
              } else {
                startNormalAudioMeter(WebRTCService.localStream);
              }
            }
          }
          WebRTCService.unlockAudio?.();
          break;
        case 'call_left':
          document.getElementById('wrapJoin').style.display = 'flex';
          document.getElementById('wrapMic').style.display = 'none';
          document.getElementById('wrapCam').style.display = 'none';
          document.getElementById('wrapScreen').style.display = 'none';
          if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
          setLocalVideo(null);
          clearAllSources();
          broadcastPresenter('none');
          stopNormalAudioMeter();
          AutoSTT.stop(); setTPStatus('idle', 'Sẵn sàng');
          break;
        case 'mic_toggled':
          micOn = ev.enabled;
          document.getElementById('micBtn').classList.toggle('off', !ev.enabled);
          document.getElementById('micLabel').textContent = ev.enabled ? 'Mic' : 'Tắt mic';
          document.getElementById('selfMicIcon').textContent = ev.enabled ? '🎤' : '🔇';
          document.getElementById('selfMicIcon').classList.toggle('muted', !ev.enabled);
          if (!ev.enabled) { 
            AutoSTT.stop(); 
            stopNormalAudioMeter();
            setTPStatus('idle', 'Mic đã tắt'); 
          }
          else if (WebRTCService.localStream) {
            if (roomMode === 'ai') {
              setTimeout(() => { if (!AutoSTT.isRunning() && roomMode === 'ai') AutoSTT.start(WebRTCService.localStream, ws, currentSpeak); }, 100);
            } else {
              startNormalAudioMeter(WebRTCService.localStream);
            }
          }
          break;
        case 'cam_toggled':
          document.getElementById('camBtn').classList.toggle('active', ev.enabled);
          document.getElementById('camBtn').textContent = ev.enabled ? '📹' : '📷';
          document.getElementById('camLabel').textContent = ev.enabled ? 'Tắt cam' : 'Camera';
          setLocalVideo(ev.enabled ? ev.stream : null);
          if (ev.enabled) {
            setLocalSources({ camera: ev.stream });
            if (!WebRTCService.isScreenOn) broadcastPresenter('camera');
          } else {
            setLocalSources({ camera: false });
            if (!WebRTCService.isScreenOn) broadcastPresenter('none');
          }
          break;
        case 'screen_toggled':
          document.getElementById('screenBtn').classList.toggle('active', ev.enabled);
          document.getElementById('screenBtn').textContent = ev.enabled ? '⏹️' : '🖥️';
          document.getElementById('screenLabel').textContent = ev.enabled ? 'Dừng' : 'Màn hình';
          if (ev.enabled) {
            setLocalSources({ screen: ev.stream });
            broadcastPresenter('screen');
          } else {
            setLocalSources({ screen: false });
            const cam = WebRTCService.localVideoStream;
            setLocalVideo(cam || null);
            if (cam) { setLocalSources({ camera: cam }); broadcastPresenter('camera'); }
            else { broadcastPresenter('none'); }
          }
          break;
        case 'peer_video':
          if (ev.peerId && !document.getElementById(`tile-${ev.peerId}`)) {
            addPeerTile(ev.peerId);
          }
          if (ev.kind === 'camera') {
            setPeerVideo(ev.peerId, ev.stream);
          }
          setPeerSource(ev.peerId, ev.stream, ev.kind);
          break;
        case 'cam_error': T.err('Lỗi camera', ev.reason); break;
        case 'screen_error': T.err('Lỗi chia sẻ màn hình', ev.reason); break;
        case 'mic_error': T.err('Lỗi micro', ev.reason); break;"""

new_rtc_event = """    function onRTCEvent(ev) {
      switch (ev.type) {
        case 'call_joined':
          document.getElementById('wrapJoin').style.display = 'none';
          document.getElementById('wrapMic').style.display = 'flex';
          document.getElementById('wrapCam').style.display = 'flex';
          document.getElementById('wrapScreen').style.display = 'flex';
          startTimer();

          // Đồng bộ theo trạng thái MediaStreamTrack thực tế
          const actualAudioTrack = WebRTCService.localStream?.getAudioTracks?.()[0];
          micOn = !!(actualAudioTrack && actualAudioTrack.readyState === 'live' && actualAudioTrack.enabled && WebRTCService.isMicOn);
          camOn = !!(WebRTCService.isCamOn && WebRTCService.localVideoStream);
          updateMediaButtonsUI();

          if (camOn && WebRTCService.localVideoStream) {
            setLocalVideo(WebRTCService.localVideoStream);
            setLocalSources({ camera: WebRTCService.localVideoStream });
            broadcastPresenter('camera');
          } else {
            setLocalVideo(null);
          }

          if (micOn && actualAudioTrack) {
            if (roomMode === 'ai') {
              AutoSTT.start(WebRTCService.localStream, ws, currentSpeak);
            } else {
              startNormalAudioMeter(WebRTCService.localStream);
            }
          } else {
            AutoSTT.stop();
            stopNormalAudioMeter();
            if (roomMode === 'ai') setTPStatus('idle', 'Mic đã tắt');
          }
          WebRTCService.unlockAudio?.();
          break;
        case 'call_left':
          document.getElementById('wrapJoin').style.display = 'flex';
          document.getElementById('wrapMic').style.display = 'none';
          document.getElementById('wrapCam').style.display = 'none';
          document.getElementById('wrapScreen').style.display = 'none';
          if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
          setLocalVideo(null);
          clearAllSources();
          broadcastPresenter('none');
          stopNormalAudioMeter();
          AutoSTT.stop(); setTPStatus('idle', 'Sẵn sàng');
          break;
        case 'mic_toggled':
          const curAudioTrack = WebRTCService.localStream?.getAudioTracks?.()[0];
          micOn = !!(ev.enabled && curAudioTrack && curAudioTrack.enabled);
          updateMediaButtonsUI();
          if (!micOn) { 
            AutoSTT.stop(); 
            stopNormalAudioMeter();
            setTPStatus('idle', 'Mic đã tắt'); 
          }
          else if (WebRTCService.localStream) {
            if (roomMode === 'ai') {
              setTimeout(() => { if (!AutoSTT.isRunning() && roomMode === 'ai') AutoSTT.start(WebRTCService.localStream, ws, currentSpeak); }, 100);
            } else {
              startNormalAudioMeter(WebRTCService.localStream);
            }
          }
          break;
        case 'cam_toggled':
          camOn = !!(ev.enabled && WebRTCService.localVideoStream);
          updateMediaButtonsUI();
          setLocalVideo(camOn ? ev.stream : null);
          if (camOn) {
            setLocalSources({ camera: ev.stream });
            if (!WebRTCService.isScreenOn) broadcastPresenter('camera');
          } else {
            setLocalSources({ camera: false });
            if (!WebRTCService.isScreenOn) broadcastPresenter('none');
          }
          break;
        case 'screen_toggled':
          document.getElementById('screenBtn').classList.toggle('active', ev.enabled);
          document.getElementById('screenBtn').textContent = ev.enabled ? '⏹️' : '🖥️';
          document.getElementById('screenLabel').textContent = ev.enabled ? 'Dừng' : 'Màn hình';
          if (ev.enabled) {
            setLocalSources({ screen: ev.stream });
            broadcastPresenter('screen');
          } else {
            setLocalSources({ screen: false });
            const cam = WebRTCService.localVideoStream;
            setLocalVideo(cam || null);
            if (cam) { setLocalSources({ camera: cam }); broadcastPresenter('camera'); }
            else { broadcastPresenter('none'); }
          }
          break;
        case 'peer_video':
          if (ev.peerId && !document.getElementById(`tile-${ev.peerId}`)) {
            addPeerTile(ev.peerId);
          }
          if (ev.kind === 'camera') {
            setPeerVideo(ev.peerId, ev.stream);
          }
          setPeerSource(ev.peerId, ev.stream, ev.kind);
          break;
        case 'cam_error':
          camOn = false;
          updateMediaButtonsUI();
          setLocalVideo(null);
          setLocalSources({ camera: false });
          T.err('Lỗi camera', ev.reason);
          break;
        case 'screen_error':
          T.err('Lỗi chia sẻ màn hình', ev.reason);
          break;
        case 'mic_error':
          micOn = false;
          updateMediaButtonsUI();
          AutoSTT.stop();
          stopNormalAudioMeter();
          T.err('Lỗi micro', ev.reason);
          break;"""

room_norm = room_code.replace("\r\n", "\n")
assert old_boot_user in room_norm, "old_boot_user not found in room.html"
room_norm = room_norm.replace(old_boot_user, new_boot_user)

assert old_boot_tile in room_norm, "old_boot_tile not found in room.html"
room_norm = room_norm.replace(old_boot_tile, new_boot_tile)

assert old_join_call_room in room_norm, "old_join_call_room not found in room.html"
room_norm = room_norm.replace(old_join_call_room, new_join_call_room)

assert old_rtc_event in room_norm, "old_rtc_event not found in room.html"
room_norm = room_norm.replace(old_rtc_event, new_rtc_event)

with open(room_path, "w", encoding="utf-8") as f:
    f.write(room_norm)
print("4. Updated room.html successfully!")
