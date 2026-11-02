# AI Meeting Translator

Hệ thống họp trực tuyến đa ngôn ngữ realtime.

## Yêu cầu
- Python 3.13+
- MySQL đang chạy

## Cài đặt và chạy (Windows)

### 1. Tạo virtual environment

```cmd
cd ai-meeting-translator\backend
python -m venv venv
venv\Scripts\activate
```

### 2. Cài dependencies

```cmd
pip install -r requirements.txt
```

### 3. Cấu hình .env

Sao chép file mẫu rồi chỉnh lại thông tin database:

```cmd
copy .env.example .env
```

Mở `.env` và sửa các giá trị:

```
DB_USER=root
DB_PASSWORD=your_password_here
DB_NAME=ai_meeting_translator
```

Tạo database trong MySQL (chưa cần tạo bảng):

```sql
CREATE DATABASE ai_meeting_translator CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 4. Chạy server

```cmd
uvicorn app.main:app --reload
```

Server sẽ chạy tại: http://localhost:8000

### 5. Kiểm tra health check

Mở trình duyệt hoặc dùng curl:

```cmd
curl http://localhost:8000/api/health
```

Kết quả mong đợi:

```json
{
  "status": "ok",
  "message": "AI Meeting Translator API is running"
}
```

Swagger UI (tài liệu API tự động): http://localhost:8000/docs
