from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "AI Meeting Translator"
    DEBUG: bool = False

    # Admin
    ADMIN_SECRET_KEY: str = "changeme-admin-secret-2026"

    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "ai_meeting_translator"

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Translation
    TRANSLATION_PROVIDER: str = "deep_google"  # nllb | marian | google | deep_google
    GOOGLE_TRANSLATE_API_KEY: str = ""

    # TTS
    TTS_PROVIDER: str = "gtts"   # gtts | edge
    TTS_ENABLED: bool = True
    TTS_TIMEOUT: int = 15        # giây, timeout cho edge-tts

    # Whisper
    WHISPER_MODEL: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    # beam_size=2: cân bằng tốt giữa tốc độ (~3x nhanh hơn beam=5) và độ chính xác trên CPU real-time
    # Tăng lên 4-5 nếu có GPU hoặc chấp nhận latency cao hơn
    WHISPER_BEAM_SIZE: int = 2
    WHISPER_TEMPERATURE: float = 0.0
    WHISPER_NO_SPEECH_PROB_MAX: float = 0.6
    WHISPER_AVG_LOGPROB_MIN: float = -1.0
    WHISPER_COMPRESSION_RATIO_MAX: float = 2.4

    # VAD
    VAD_THRESHOLD: float = 0.3
    VAD_MIN_SPEECH_MS: int = 500
    VAD_MIN_SILENCE_MS: int = 800
    VAD_SPEECH_PAD_MS: int = 200

    # WebRTC ICE Servers
    # STUN — dùng Google STUN mặc định, có thể override
    STUN_URLS: str = "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    # TURN — để trống nếu không có TURN server
    TURN_URL: str = ""
    TURN_USERNAME: str = ""
    TURN_CREDENTIAL: str = ""

    @property
    def ice_servers(self) -> list[dict]:
        servers = [{"urls": u.strip()} for u in self.STUN_URLS.split(",") if u.strip()]
        if self.TURN_URL:
            servers.append({
                "urls": self.TURN_URL,
                "username": self.TURN_USERNAME,
                "credential": self.TURN_CREDENTIAL,
            })
        return servers

    # SQLite fallback: đặt DB_SQLITE=1 trong .env để dùng SQLite thay MySQL
    DB_SQLITE: bool = False

    @property
    def DATABASE_URL(self) -> str:
        if self.DB_SQLITE:
            return f"sqlite:///./ai_meeting.db"
        return (f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
                f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    class Config:
        env_file = ".env"


settings = Settings()
