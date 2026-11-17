"""
Gunicorn configuration for LinguaMeet Production.
LƯU Ý QUAN TRỌNG:
Chỉ sử dụng duy nhất 1 worker (workers = 1) vì:
1. ConnectionManager quản lý WebRTC multi-room state in-memory.
2. WhisperService serialize CPU model qua threading.Lock singleton.
Nếu chạy nhiều worker process độc lập, state giữa các phòng họp sẽ bị phân mảnh.
"""
import multiprocessing

bind = "127.0.0.1:8000"
workers = 1  # BẮT BUỘC: 1 worker duy nhất
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 120
keepalive = 5
max_requests = 10000
max_requests_jitter = 500

accesslog = "/var/log/linguameet/access.log"
errorlog = "/var/log/linguameet/error.log"
loglevel = "info"
capture_output = True
