"""
languages.py

Maps the display names shown in the UI (flags + English names) to the
locale codes each engine actually expects. Azure wants BCP-47 speech
locales for "from" (e.g. "en-US") and short ISO codes for "to"
(e.g. "fa"). Argos Translate and faster-whisper want plain ISO 639-1
codes on both sides, so we normalize through this one table instead of
scattering string constants across engines.

Usage:
    from languages import LANGUAGES, to_azure_speech_locale, to_iso639
    LANGUAGES["Persian (Farsi)"]        # -> {"flag": "🇮🇷", "iso": "fa", "azure_target": "fa"}
    to_azure_speech_locale("English")   # -> "en-US"
"""

LANGUAGES = {
    "English": {"flag": "🇺🇸", "iso": "en", "azure_speech_locale": "en-US", "azure_target": "en"},
    "Persian (Farsi)": {"flag": "🇮🇷", "iso": "fa", "azure_speech_locale": "fa-IR", "azure_target": "fa"},
    "Spanish": {"flag": "🇪🇸", "iso": "es", "azure_speech_locale": "es-ES", "azure_target": "es"},
    "French": {"flag": "🇫🇷", "iso": "fr", "azure_speech_locale": "fr-FR", "azure_target": "fr"},
    "German": {"flag": "🇩🇪", "iso": "de", "azure_speech_locale": "de-DE", "azure_target": "de"},
    "Arabic": {"flag": "🇸🇦", "iso": "ar", "azure_speech_locale": "ar-SA", "azure_target": "ar"},
    "Mandarin Chinese": {"flag": "🇨🇳", "iso": "zh", "azure_speech_locale": "zh-CN", "azure_target": "zh-Hans"},
    "Turkish": {"flag": "🇹🇷", "iso": "tr", "azure_speech_locale": "tr-TR", "azure_target": "tr"},
}


def to_azure_speech_locale(display_name: str) -> str:
    """
    to_azure_speech_locale(display_name)
    Usage: pass a UI display name like "English" to get the locale Azure's
    SpeechTranslationConfig expects for `speech_recognition_language`
    (e.g. "en-US"). Falls back to "en-US" for unknown names.
    """
    return LANGUAGES.get(display_name, LANGUAGES["English"])["azure_speech_locale"]


def to_azure_target(display_name: str) -> str:
    """
    to_azure_target(display_name)
    Usage: pass a UI display name to get the short code Azure expects in
    `add_target_language(...)` for the translation output (e.g. "fa").
    """
    return LANGUAGES.get(display_name, LANGUAGES["Persian (Farsi)"])["azure_target"]


def to_iso639(display_name: str) -> str:
    """
    to_iso639(display_name)
    Usage: pass a UI display name to get the plain ISO 639-1 code used by
    the offline engine (faster-whisper language hint, Argos Translate
    from_code/to_code).
    """
    return LANGUAGES.get(display_name, LANGUAGES["English"])["iso"]
