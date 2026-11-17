"""
Database Backup & Rotation Script for LinguaMeet.
Supports both MySQL (via mysqldump or direct python export) and SQLite.
"""
import os
import sys
import time
import shutil
import subprocess
import argparse
from datetime import datetime

# Đảm bảo import được config từ backend
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.config import settings


def ensure_backup_dir(backup_dir: str) -> str:
    abs_dir = os.path.abspath(backup_dir)
    os.makedirs(abs_dir, exist_ok=True)
    return abs_dir


def rotate_backups(backup_dir: str, keep_count: int, prefix: str):
    """Giữ lại N bản sao lưu mới nhất, dọn dẹp các bản cũ hơn."""
    files = []
    for f in os.listdir(backup_dir):
        if f.startswith(prefix) and (f.endswith(".sql") or f.endswith(".db")):
            full_path = os.path.join(backup_dir, f)
            files.append((os.path.getmtime(full_path), full_path))

    files.sort(key=lambda x: x[0], reverse=True)
    if len(files) > keep_count:
        for _, path_to_remove in files[keep_count:]:
            try:
                os.remove(path_to_remove)
                print(f"[Backup] Rotated old backup: {os.path.basename(path_to_remove)}")
            except Exception as e:
                print(f"[Backup] Failed to remove {path_to_remove}: {e}")


def backup_sqlite(backup_dir: str) -> str:
    """Sao lưu file database SQLite an toàn."""
    db_file = os.path.join(_backend_dir, "ai_meeting.db")
    if not os.path.exists(db_file):
        raise FileNotFoundError(f"Không tìm thấy file SQLite tại {db_file}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_file = os.path.join(backup_dir, f"backup_sqlite_{timestamp}.db")
    shutil.copy2(db_file, target_file)
    return target_file


def backup_mysql(backup_dir: str) -> str:
    """Sao lưu database MySQL bằng mysqldump hoặc Python exporter."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_file = os.path.join(backup_dir, f"backup_mysql_{settings.DB_NAME}_{timestamp}.sql")

    mysqldump_bin = shutil.which("mysqldump")
    if not mysqldump_bin:
        for p in [r"C:\xampp\mysql\bin\mysqldump.exe", r"D:\xampp\mysql\bin\mysqldump.exe", "/usr/bin/mysqldump"]:
            if os.path.isfile(p):
                mysqldump_bin = p
                break

    if mysqldump_bin:
        cmd = [
            mysqldump_bin,
            f"--host={settings.DB_HOST}",
            f"--port={settings.DB_PORT}",
            f"--user={settings.DB_USER}",
        ]
        if settings.DB_PASSWORD:
            cmd.append(f"--password={settings.DB_PASSWORD}")
        cmd.extend([
            "--routines",
            "--triggers",
            "--single-transaction",
            settings.DB_NAME,
        ])

        with open(target_file, "w", encoding="utf-8") as out_f:
            res = subprocess.run(cmd, stdout=out_f, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                os.remove(target_file)
                raise RuntimeError(f"mysqldump error: {res.stderr}")
    else:
        # Fallback bằng SQLAlchemy metadata dump nếu không có mysqldump client
        from app.database import engine, Base
        import app.models  # noqa
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(f"-- LinguaMeet Python DB Export at {timestamp}\n")
            f.write(f"-- Database: {settings.DB_NAME}\n\n")
            with engine.connect() as conn:
                for table in reversed(Base.metadata.sorted_tables):
                    f.write(f"-- Table: {table.name}\n")
                    rows = conn.execute(table.select()).mappings().all()
                    for r in rows:
                        cols = ", ".join(r.keys())
                        vals = ", ".join([repr(v) if v is not None else "NULL" for v in r.values()])
                        f.write(f"INSERT INTO {table.name} ({cols}) VALUES ({vals});\n")
                    f.write("\n")

    return target_file


def run_backup(output_dir: str = "backups", keep: int = 14):
    target_dir = ensure_backup_dir(output_dir)
    print(f"[Backup] Starting database backup at {datetime.now().isoformat()}...")

    if settings.DB_SQLITE:
        file_path = backup_sqlite(target_dir)
        prefix = "backup_sqlite_"
    else:
        file_path = backup_mysql(target_dir)
        prefix = f"backup_mysql_{settings.DB_NAME}_"

    size_kb = os.path.getsize(file_path) / 1024
    print(f"[Backup] Backup created successfully: {file_path} ({size_kb:.2f} KB)")

    rotate_backups(target_dir, keep, prefix)
    print(f"[Backup] Completed. Kept latest {keep} backups.")
    return file_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LinguaMeet Database Backup Tool")
    parser.add_argument("--output-dir", default=os.path.join(_backend_dir, "backups"), help="Thư mục lưu backup")
    parser.add_argument("--keep", type=int, default=14, help="Số lượng bản sao lưu lưu trữ tối đa")
    args = parser.parse_args()

    try:
        run_backup(args.output_dir, args.keep)
    except Exception as exc:
        print(f"[Backup Error] {exc}", file=sys.stderr)
        sys.exit(1)
