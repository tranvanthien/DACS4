"""
Danh sách ngôn ngữ hỗ trợ và các helper function.
Đây là single source of truth cho toàn bộ project.
"""

LANGUAGES: dict[str, dict] = {
    "vi": {"name": "Tiếng Việt", "flag": "🇻🇳"},
    "en": {"name": "English",    "flag": "🇬🇧"},
    "ko": {"name": "한국어",      "flag": "🇰🇷"},
    "ja": {"name": "日本語",      "flag": "🇯🇵"},
    "zh": {"name": "中文",        "flag": "🇨🇳"},
    "fr": {"name": "Français",   "flag": "🇫🇷"},
    "de": {"name": "Deutsch",    "flag": "🇩🇪"},
    "es": {"name": "Español",    "flag": "🇪🇸"},
}

# Map tên dài → code ngắn (để normalize)
_NAME_TO_CODE: dict[str, str] = {
    "vietnamese": "vi", "tiếng việt": "vi",
    "english": "en",
    "korean": "ko", "한국어": "ko",
    "japanese": "ja", "日本語": "ja",
    "chinese": "zh", "中文": "zh",
    "french": "fr", "français": "fr",
    "german": "de", "deutsch": "de",
    "spanish": "es", "español": "es",
}


def is_supported_language(code: str) -> bool:
    return code.lower() in LANGUAGES


def normalize_language_code(value: str) -> str:
    """Chuyển tên dài hoặc code về code chuẩn (2 ký tự)."""
    v = value.strip().lower()
    if v in LANGUAGES:
        return v
    return _NAME_TO_CODE.get(v, v)


def get_language_name(code: str) -> str:
    return LANGUAGES.get(code, {}).get("name", code)


def get_all_languages() -> list[dict]:
    return [
        {"code": code, "name": info["name"], "flag": info["flag"]}
        for code, info in LANGUAGES.items()
    ]
