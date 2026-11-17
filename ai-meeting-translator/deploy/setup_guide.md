# HƯỚNG DẪN TRIỂN KHAI PRODUCTION LINGUAMEET TRÊN VPS LINUX

> Áp dụng cho: Ubuntu 22.04 / 24.04 LTS hoặc Debian 12  
> Yêu cầu: 4 vCPU, 8GB RAM, 50GB SSD, Domain trỏ về IP VPS.

---

## 1. Cài đặt các gói phụ thuộc hệ thống
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git ffmpeg nginx certbot python3-certbot-nginx coturn mysql-server
```

---

## 2. Thiết lập Database MySQL
```bash
sudo mysql -u root
```
```sql
CREATE DATABASE ai_meeting_translator CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'linguameet'@'localhost' IDENTIFIED BY 'MatKhauManh2026!@#';
GRANT ALL PRIVILEGES ON ai_meeting_translator.* TO 'linguameet'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

---

## 3. Cài đặt mã nguồn và môi trường ảo Python
```bash
sudo mkdir -p /var/www/linguameet
sudo chown -R $USER:$USER /var/www/linguameet
cd /var/www/linguameet
git clone <URL_REPO> .

cd ai-meeting-translator/backend
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

Cấu hình file `.env`:
```bash
cp .env.example .env
nano .env
```
*(Điền DB_USER, DB_PASSWORD, JWT_SECRET_KEY, CORS_ORIGINS, TURN_URL)*

---

## 4. Cấu hình dịch vụ Systemd tự khởi động
```bash
sudo mkdir -p /var/log/linguameet
sudo chown -R www-data:www-data /var/log/linguameet

sudo cp /var/www/linguameet/ai-meeting-translator/deploy/linguameet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable linguameet
sudo systemctl start linguameet
sudo systemctl status linguameet
```

---

## 5. Cấu hình Nginx & Cấp chứng chỉ SSL HTTPS
```bash
sudo cp /var/www/linguameet/ai-meeting-translator/deploy/nginx.conf /etc/nginx/sites-available/linguameet.conf
# Sửa your-domain.com thành domain thật
sudo nano /etc/nginx/sites-available/linguameet.conf

sudo ln -s /etc/nginx/sites-available/linguameet.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Cấp chứng chỉ SSL Let's Encrypt miễn phí
sudo certbot --nginx -d your-domain.com -d www.your-domain.com
```

---

## 6. Cấu hình Coturn TURN Server (WebRTC NAT Traversal)
```bash
sudo cp /var/www/linguameet/ai-meeting-translator/deploy/turnserver.conf /etc/turnserver.conf
# Sửa domain và password trong file turnserver.conf
sudo nano /etc/turnserver.conf

# Kích hoạt Coturn
sudo sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn
sudo systemctl restart coturn
sudo systemctl enable coturn

# Mở Firewall trên VPS:
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 3478/tcp
sudo ufw allow 3478/udp
sudo ufw allow 5349/tcp
sudo ufw allow 5349/udp
sudo ufw allow 49152:65535/udp
```

---

## 7. Thiết lập sao lưu tự động hàng ngày (Cron Job)
```bash
crontab -e
```
Thêm dòng sau để tự động sao lưu database vào lúc 2:00 sáng mỗi ngày:
```cron
0 2 * * * /var/www/linguameet/ai-meeting-translator/backend/scripts/backup_db.sh >> /var/log/linguameet/backup.log 2>&1
```
