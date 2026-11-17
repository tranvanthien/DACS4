# BÁO CÁO ĐÁNH GIÁ DỰ ÁN LINGUAMEET — SẴN SÀNG PRODUCTION

> Ngày đánh giá: 09/11/2026  
> Quy mô mục tiêu: <100 tài khoản, nhiều phòng họp đồng thời, truy cập Internet công khai

---

## 1. NHỮNG GÌ ĐÃ HOẠT ĐỘNG TỐT

### Kiến trúc tổng thể
- Phân tách trách nhiệm rõ ràng: routers → services → models
- Lifespan management đúng chuẩn FastAPI (startup/shutdown tasks)
- ConnectionManager singleton xử lý multi-room tốt, tự dọn dẹp dead sockets
- Host grace period (15s) thông minh, tránh đóng phòng khi host reload trang
- SQLite fallback (`DB_SQLITE=1`) tiện cho development

### WebRTC
- Perfect Negotiation (Polite Peer) xử lý glare triệt để
- ICE candidate buffering trước khi remote description sẵn sàng
- Dedicated senders cho camera và screen share tách biệt
- ICE restart tự động khi connection failed
- Dynamic ICE server config từ backend API (`/api/webrtc/ice-config`)

### AI Pipeline
- Whisper serialize qua `threading.Lock` — an toàn multi-request
- VAD double-check: Silero + energy fallback, tránh false-negative với WebRTC audio
- Translation cache LRU 500 entries, tiết kiệm API call đáng kể
- TTS cache 200 entries, cùng text không gọi lại provider
- Translation thread pool 1 worker — tránh 429 từ Google gtx
- gtx 60s block khi bị 429, chuyển MyMemory fallback tự động
- TTS chạy trong thread pool riêng, không block event loop
- Translation pipeline gửi text ngay, TTS gửi sau — UX tốt

### Monitoring & Logging
- AI metrics snapshot mỗi 60s vào database
- Admin dashboard với realtime metrics (CPU, RAM, latency)
- SystemLog phân loại theo service và level
- Admin API test on-demand từng service AI

### Bảo mật cơ bản
- Bcrypt cho password (`bcrypt 4.2.0`)
- Email validator
- Input length check cho chat message (2000 ký tự)
- XSS protection trong frontend qua `esc()` ở mọi nơi render text
- WebSocket validate room + user + membership trước khi accept
- Admin require cả email + `is_admin` + secret key

---

## 2. LỖI VÀ RỦI RO CẦN SỬA

### 🔴 NGHIÊM TRỌNG — Phải sửa trước khi public

**[SEC-1] Không có JWT/Session — mọi API đều không xác thực người dùng thật**
- Tất cả REST endpoint nhận `user_id` và `host_id` qua query parameter hoặc request body, hoàn toàn không xác thực.
- Bất kỳ ai cũng có thể gửi `POST /api/rooms/{code}/close?host_id=1` để đóng phòng của người khác, hoặc `PUT /api/users/1/languages` để sửa thông tin bất kỳ user nào.
- Tương tự `GET /api/users/{user_id}`, `PUT /api/users/{user_id}/password` — không có auth guard.

**[SEC-2] WebSocket endpoint không xác thực token**
- `ws://host/ws/rooms/{room_code}/{user_id}` — chỉ kiểm tra user tồn tại trong DB, không có token. Ai biết `user_id` đều có thể kết nối giả mạo.

**[SEC-3] CORS wildcard `allow_origins=["*"]`**
- `main.py` hardcode `allow_origins=["*"]` — bỏ qua `CORS_ORIGINS` trong config. Với `allow_credentials=False` thì cookie bị chặn nhưng vẫn cho phép mọi origin gọi API.

**[SEC-4] Admin secret key trả về cho client khi login**
- `admin_login` trả `"admin_key": settings.ADMIN_SECRET_KEY` trong response JSON. Frontend lưu key này vào localStorage — bị lộ qua browser storage và DevTools.

**[SEC-5] `ADMIN_SECRET_KEY` mặc định hardcode trong code**
- `config.py` hardcode `ADMIN_SECRET_KEY: str = "changeme-admin-secret-2026"`. Nếu `.env` không đặt lại, server production chạy với key mặc định này.

**[SEC-6] Đăng ký không giới hạn (open registration)**
- `/api/auth/register` không có rate limiting, không có email verification, không có captcha. Bot có thể tạo hàng nghìn tài khoản.

**[SEC-7] Translation và TTS phụ thuộc external API không chính thức**
- `deep_google` dùng `translate.googleapis.com/translate_a/single` — endpoint không chính thức của Google. Google có thể block IP VPS bất cứ lúc nào, không có SLA.
- `gtts` và `edge-tts` cũng dùng API không chính thức. Không phù hợp cho production có SLA.

---

### 🟡 QUAN TRỌNG — Sửa trước khi go-live

**[PERF-1] Whisper `small` model trên CPU quá chậm cho real-time**
- Config hiện tại dùng `small` (3-7s/utterance trên CPU i7). `.env.example` khuyến nghị `base` (1-2s). Nhiều phòng đồng thời sẽ xếp hàng dài chờ lock.

**[PERF-2] Whisper `threading.Lock` serialize — bottleneck rõ ràng**
- Khi 3-4 phòng AI mode cùng lúc, `wait_ms` có thể lên đến 10-20s khi nhiều người nói đồng thời.

**[PERF-3] Translation executor 1 worker**
- `ThreadPoolExecutor(max_workers=1)` đảm bảo không bị 429 nhưng cũng serialize dịch thuật. Nhiều phòng = hàng đợi dịch dài.

**[PERF-4] Silero VAD load từ `torch.hub` khi runtime**
- `_load_silero()` gọi `torch.hub.load` từ internet lần đầu. Trên VPS không có cache, mỗi restart sẽ tải lại model từ mạng.

**[INFRA-1] Không có TURN server**
- `TURN_URL=""` trong config. Người dùng khác mạng (Internet thực) sau symmetric NAT (phổ biến với mobile 4G, nhiều ISP) sẽ không kết nối được WebRTC P2P. Đây là lỗi nghiêm trọng cho ứng dụng public.

**[INFRA-2] Không có process manager**
- Không có Gunicorn config hay systemd service. Server crash = downtime cho đến khi restart thủ công.

**[INFRA-3] Không có HTTPS**
- `getUserMedia()` (microphone) chỉ hoạt động trên HTTPS hoặc localhost theo tiêu chuẩn browser. Người dùng truy cập qua IP VPS HTTP sẽ không dùng được mic. `webrtc-service.js` đã có check này nhưng chỉ degrade gracefully thay vì báo lỗi rõ ràng.

**[DB-1] `Base.metadata.create_all()` trong production**
- `main.py` dùng `create_all` khi startup — không kiểm soát được migration. Không có Alembic.

**[DB-2] Không có database backup tự động**
- Không có backup script hay scheduled job nào trong codebase.

**[DB-3] Connection pool không cấu hình cho production**
- `create_engine` không có `pool_size`, `max_overflow`, `pool_timeout`. Default SQLAlchemy (`pool_size=5`) có thể không đủ cho nhiều concurrent WebSocket request.

---

### 🟢 TRUNG BÌNH — Cải thiện sau go-live

**[SEC-8] Không có rate limiting trên bất kỳ endpoint nào**
- `/api/auth/login` có thể bị brute force. Không có `slowapi` hay middleware rate limit.

**[PRIVACY-1] `GET /api/rooms` public, không cần auth**
- Trả 100 phòng đầu với `room_code`. Người ngoài có thể dò và tham gia phòng bất kỳ nếu biết code.

**[LOGGING-1] `print()` thay vì `logging` trong nhiều service**
- `whisper_service.py`, `vad_service.py`, `transcription_service.py` dùng `print()`. Không thể cấu hình log level, rotate file, hay gửi đến external logging service.

**[CLEANUP-1] ffmpeg path hardcode Windows**
- `transcription_service.py` có `r"D:\ffmpeg\bin\ffmpeg.exe"`. Trên Linux VPS chỉ dựa vào `shutil.which("ffmpeg")` — cần đảm bảo ffmpeg được cài.

**[CLEANUP-2] Duplicate import trong `websocket.py`**
- `from app.services import chat_service` bị import 2 lần liên tiếp.

---

## 3. THÀNH PHẦN NÊN GIỮ NGUYÊN

| Thành phần | Lý do giữ |
|---|---|
| `ConnectionManager` | Logic multi-room tốt, clean disconnect, không cần thay |
| `translation_pipeline.py` | Parallel translate + TTS, semaphore control tốt |
| `WhisperService` | Lock-based serialize đúng cách cho CPU single-model |
| WebRTC signaling relay | Perfect Negotiation chuẩn, ICE buffering tốt |
| AI monitor + metrics | Đủ dùng cho quy mô dưới 100 user |
| Admin router | Dashboard khá hoàn chỉnh, chỉ cần thêm auth guard tốt hơn |
| Schema/Model database | Cấu trúc đơn giản, phù hợp quy mô hiện tại |
| TTS/Translation cache | Giảm API call đáng kể |
| VAD double-check logic | Energy fallback tránh false-negative với WebRTC audio |

---

## 4. ĐỀ XUẤT CẤU HÌNH VPS KHỞI ĐẦU

### Tối thiểu — chỉ normal meeting, AI mode giới hạn
- **4 vCPU, 8GB RAM, 50GB SSD**
- ~$20-30/tháng (DigitalOcean, Vultr, Hetzner)

### Khuyến nghị — AI mode ổn định, 5-10 phòng đồng thời
- **8 vCPU, 16GB RAM, 80GB SSD**
- ~$60-80/tháng
- Whisper `base` ~1GB RAM; `small` ~2GB; Silero ~500MB; PyTorch ~1GB overhead

### Dịch vụ bổ sung bắt buộc

| Dịch vụ | Lý do | Chi phí ước tính |
|---|---|---|
| TURN server (Coturn) | WebRTC qua NAT/mobile | ~$5-10/tháng VPS riêng, hoặc Metered.ca ~$0.4/GB relay |
| Domain + SSL | `getUserMedia` cần HTTPS | Domain ~$10/năm, SSL miễn phí qua Let's Encrypt |

---

## 5. ĐỀ XUẤT KIẾN TRÚC TRIỂN KHAI PRODUCTION

```
Internet
   │
   ▼
[Nginx]  ← HTTPS/WSS termination, reverse proxy, rate limiting, static files
   │
   ├── /api/*   →  FastAPI (Gunicorn + UvicornWorker, 1 worker)
   ├── /ws/*    →  FastAPI WebSocket (cùng process)
   └── /ui/*    →  Static frontend files
   │
   ├── MySQL (local hoặc managed DB)
   └── Coturn TURN server (port 3478 UDP/TCP, 5349 TLS)
```

**Lưu ý quan trọng:** Chỉ dùng **1 Gunicorn worker** vì `WhisperService` và `ConnectionManager` là singleton in-memory. Multi-process sẽ tạo nhiều instance độc lập, mất đồng bộ state phòng họp.

**Nginx WebSocket config cần thiết:**
```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 86400;
```

---

## 6. PHÂN CHIA CÔNG VIỆC THEO GIAI ĐOẠN

### Giai đoạn 1 — Bảo mật cơ bản 🔴
*Ước tính: 1-2 tuần | Bắt buộc trước khi public*

| # | Công việc | Độ khó | Phụ thuộc |
|---|---|---|---|
| 1 | Thêm JWT authentication — issue token khi login, verify trên mọi API | Cao | Refactor toàn bộ routers |
| 2 | WebSocket token authentication (query param `?token=...`) | Trung bình | Hoàn thành JWT |
| 3 | Sửa CORS dùng `cors_origins_list` từ config thay vì wildcard | Thấp | Không |
| 4 | Bỏ trả `admin_key` trong response, dùng session token riêng cho admin | Trung bình | Không |
| 5 | Cấu hình Nginx + Let's Encrypt SSL trên VPS | Thấp | Có domain |

### Giai đoạn 2 — Infrastructure 🟡
*Ước tính: 1 tuần | Cần trước go-live*

| # | Công việc | Độ khó | Phụ thuộc |
|---|---|---|---|
| 6 | Cài Coturn TURN server, cấu hình `TURN_URL` trong `.env` | Thấp | VPS với UDP port mở |
| 7 | Cài Gunicorn + systemd service cho auto-restart | Thấp | Không |
| 8 | Thêm Alembic cho database migrations | Trung bình | Review schema trước |
| 9 | Script backup MySQL tự động (cron + mysqldump) | Thấp | Không |
| 10 | Đổi `WHISPER_MODEL=base` trong production `.env` | Rất thấp | Không |

### Giai đoạn 3 — Hiệu năng & Stability 🟢
*Ước tính: 1-2 tuần | Sau launch*

| # | Công việc | Độ khó | Phụ thuộc |
|---|---|---|---|
| 11 | Thêm rate limiting với `slowapi` (login, register, API) | Thấp | Không |
| 12 | Pre-download Silero model vào startup script | Thấp | Không |
| 13 | Cấu hình connection pool `pool_size=10, max_overflow=20` | Rất thấp | Không |
| 14 | Thay `print()` bằng `logging` proper trong AI services | Thấp | Không |
| 15 | Thêm auth guard cho `GET /api/rooms` | Thấp | Hoàn thành JWT |
| 16 | Sửa duplicate import và ffmpeg path hardcode | Rất thấp | Không |

### Giai đoạn 4 — Mở rộng ⚪
*Tùy nhu cầu thực tế*

| # | Công việc | Độ khó | Trigger |
|---|---|---|---|
| 17 | Whisper microservice riêng với Redis queue | Cao | >20 phòng AI đồng thời |
| 18 | Chuyển sang SFU (mediasoup hoặc LiveKit) | Rất cao | >50 người/phòng hoặc P2P quá tải |
| 19 | Thay translation provider bằng DeepL API hoặc Google Cloud Translation v2 | Trung bình | gtx/MyMemory bị block hoặc cần SLA |

---

## TÓM TẮT

Dự án có nền tảng kỹ thuật **tốt và hoàn chỉnh về chức năng** — WebRTC, AI pipeline, admin panel đều đã hoạt động ổn định. Vấn đề chính là **chưa có authentication/authorization thực sự** cho user thông thường — đây là gap lớn nhất trước khi public. TURN server và HTTPS cũng bắt buộc cho người dùng Internet thực.

Với khoảng **3-4 tuần** xử lý 3 giai đoạn đầu, dự án đủ điều kiện triển khai cho 100 người dùng đầu tiên.
