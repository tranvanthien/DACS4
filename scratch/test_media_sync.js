/**
 * test_media_sync.js
 * Test suite kiểm thử đồng bộ trạng thái Camera và Micro giữa Lobby và Meeting Room
 * Bao gồm đủ 4 trường hợp theo yêu cầu.
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

console.log('=== TEST 1: Kiểm tra cú pháp và tham số goRoom trong user.js ===');
const userJsCode = fs.readFileSync(path.join(__dirname, '../ai-meeting-translator/frontend/user/user.js'), 'utf-8');

// Tạo mock context cho user.js
const mockWindow = {
    location: { protocol: 'http:', host: 'localhost:8000', port: '8000', href: '' }
};
const ctx = {
    window: mockWindow,
    location: mockWindow.location,
    URLSearchParams: URLSearchParams,
    sessionStorage: {
        _store: {},
        setItem(k, v) { this._store[k] = String(v); },
        getItem(k) { return this._store[k] ?? null; },
        removeItem(k) { delete this._store[k]; }
    },
    document: { addEventListener: () => {} },
    console: console
};
vm.createContext(ctx);
vm.runInContext(userJsCode, ctx);

assert(typeof ctx.goRoom === 'function', 'goRoom must be a function');

// Test 4 kịch bản goRoom:
const testCases = [
    { name: 'Cam ON, Mic ON', mic: true, cam: true, expMic: '1', expCam: '1' },
    { name: 'Cam ON, Mic OFF', mic: false, cam: true, expMic: '0', expCam: '1' },
    { name: 'Cam OFF, Mic ON', mic: true, cam: false, expMic: '1', expCam: '0' },
    { name: 'Cam OFF, Mic OFF', mic: false, cam: false, expMic: '0', expCam: '0' },
];

testCases.forEach(tc => {
    ctx.goRoom('ROOM123', 42, 'vi', 'en', 'TestUser', tc.mic, tc.cam);
    const url = new URL('http://localhost/' + ctx.location.href);
    assert.strictEqual(url.searchParams.get('room'), 'ROOM123');
    assert.strictEqual(url.searchParams.get('userId'), '42');
    assert.strictEqual(url.searchParams.get('mic'), tc.expMic, `${tc.name}: mic param must be ${tc.expMic}`);
    assert.strictEqual(url.searchParams.get('cam'), tc.expCam, `${tc.name}: cam param must be ${tc.expCam}`);
    console.log(`  ✓ ${tc.name} -> URL: ${ctx.location.href}`);
});

console.log('\n=== TEST 2: Kiểm tra webrtc-service.js với 4 kịch bản khởi tạo initialMic & initialCam ===');
const webrtcCode = fs.readFileSync(path.join(__dirname, '../ai-meeting-translator/frontend/webrtc-service.js'), 'utf-8');

// Mock WebRTC browser APIs
class MockAudioTrack {
    constructor() {
        this.kind = 'audio';
        this.id = 'audio-trk-1';
        this.enabled = true;
        this.readyState = 'live';
    }
    stop() { this.readyState = 'ended'; }
}
class MockVideoTrack {
    constructor(id = 'video-trk-1') {
        this.kind = 'video';
        this.id = id;
        this.enabled = true;
        this.readyState = 'live';
        this.contentHint = '';
    }
    stop() { this.readyState = 'ended'; }
}
class MockMediaStream {
    constructor(tracks = []) {
        this._tracks = tracks;
        this.id = 'stream-' + Math.random();
    }
    getAudioTracks() { return this._tracks.filter(t => t.kind === 'audio'); }
    getVideoTracks() { return this._tracks.filter(t => t.kind === 'video'); }
    getTracks() { return this._tracks; }
}

async function runWebRTCTest(scenario) {
    let audioTrackCreated = null;
    let videoTrackCreated = null;

    const mockNavigator = {
        mediaDevices: {
            getUserMedia: async (constraints) => {
                if (constraints.audio) {
                    audioTrackCreated = new MockAudioTrack();
                    return new MockMediaStream([audioTrackCreated]);
                }
                if (constraints.video) {
                    videoTrackCreated = new MockVideoTrack();
                    return new MockMediaStream([videoTrackCreated]);
                }
                throw new Error('Unknown constraints');
            }
        }
    };

    const mockWs = {
        readyState: 1, // OPEN
        send: () => {}
    };

    const wCtx = {
        window: { isSecureContext: true },
        WebSocket: { OPEN: 1 },
        navigator: mockNavigator,
        MediaStream: MockMediaStream,
        RTCPeerConnection: class {
            constructor() {}
            getSenders() { return []; }
            addTrack() { return { replaceTrack: async () => {} }; }
        },
        RTCIceCandidate: class {},
        RTCSessionDescription: class {},
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        console: { log: () => {}, warn: () => {}, error: () => {} },
        setTimeout: (fn) => setTimeout(fn, 10),
        clearTimeout: (id) => clearTimeout(id)
    };

    vm.createContext(wCtx);
    // Execute module wrapper and expose to global
    vm.runInContext(webrtcCode + '\nthis.WebRTCService = WebRTCService;', wCtx);
    const service = wCtx.WebRTCService;
    service.setWebSocket(mockWs);

    let eventEmitted = null;
    service.onStateChange = (ev) => {
        if (ev.type === 'call_joined') {
            eventEmitted = ev;
        }
    };

    await service.joinCall(100, [], {
        initialMic: scenario.mic,
        initialCam: scenario.cam
    });

    // 1. Kiểm tra trạng thái isMicOn và MediaStreamTrack.enabled
    assert.strictEqual(service.isMicOn, scenario.mic, `isMicOn should match scenario (${scenario.mic})`);
    assert(audioTrackCreated, 'Audio track must have been acquired');
    assert.strictEqual(audioTrackCreated.enabled, scenario.mic, `actual audioTrack.enabled must be ${scenario.mic}`);

    // 2. Kiểm tra trạng thái isCamOn và Video track
    assert.strictEqual(service.isCamOn, scenario.cam, `isCamOn should match scenario (${scenario.cam})`);
    if (scenario.cam) {
        assert(videoTrackCreated, 'Video track must have been acquired when cam is ON');
        assert.strictEqual(videoTrackCreated.readyState, 'live');
    } else {
        assert.strictEqual(videoTrackCreated, null, 'Video track must NOT be acquired when cam is OFF');
    }

    // 3. Kiểm tra event call_joined
    assert(eventEmitted, 'call_joined event must be emitted');
    assert.strictEqual(eventEmitted.micOn, scenario.mic);
    assert.strictEqual(eventEmitted.camOn, scenario.cam);

    // 4. Kiểm tra toggle mic trong cuộc gọi
    const nextMic = service.toggleMic();
    assert.strictEqual(nextMic, !scenario.mic, 'toggleMic should toggle state');
    assert.strictEqual(audioTrackCreated.enabled, !scenario.mic, 'audioTrack.enabled should follow toggle');

    // 5. Rời cuộc gọi dọn dẹp
    service.leaveCall();
    console.log(`  ✓ Kịch bản ${scenario.name}: initialMic=${scenario.mic}, initialCam=${scenario.cam} -> ĐẠT!`);
}

(async () => {
    for (const tc of testCases) {
        await runWebRTCTest(tc);
    }

    console.log('\n=== TEST 3: Kiểm tra xử lý từ chối quyền microphone ===');
    const wCtxDenied = {
        window: { isSecureContext: true },
        WebSocket: { OPEN: 1 },
        navigator: {
            mediaDevices: {
                getUserMedia: async (constraints) => {
                    if (constraints.audio) {
                        const err = new Error('Permission denied');
                        err.name = 'NotAllowedError';
                        throw err;
                    }
                }
            }
        },
        MediaStream: MockMediaStream,
        RTCPeerConnection: class {},
        RTCIceCandidate: class {},
        RTCSessionDescription: class {},
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        console: { log: () => {}, warn: () => {}, error: () => {} },
        setTimeout: (fn) => setTimeout(fn, 10),
        clearTimeout: (id) => clearTimeout(id)
    };
    vm.createContext(wCtxDenied);
    vm.runInContext(webrtcCode + '\nthis.WebRTCService = WebRTCService;', wCtxDenied);
    const deniedService = wCtxDenied.WebRTCService;
    deniedService.setWebSocket({ readyState: 1, send: () => {} });

    let micErrorEmitted = false;
    deniedService.onStateChange = (ev) => {
        if (ev.type === 'mic_error') micErrorEmitted = true;
    };

    // joinCall không được throw làm sập app khi bị từ chối quyền mic
    await deniedService.joinCall(101, [], { initialMic: true, initialCam: false });
    assert.strictEqual(micErrorEmitted, true, 'mic_error event must be emitted');
    assert.strictEqual(deniedService.isMicOn, false, 'isMicOn must be false');
    assert.strictEqual(deniedService.isInCall, true, 'User should still be in call despite mic error');
    console.log('  ✓ Xử lý từ chối quyền micro mượt mà, không sập ứng dụng!');

    console.log('\n=== TẤT CẢ CÁC BÀI KIỂM THỬ ĐỒNG BỘ CAMERA & MICRO ĐÃ VƯỢT QUA 100% ===');
})();
