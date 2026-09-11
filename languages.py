"""
languages.py

Maps the display names shown in the UI (flags + English names) to the
locale codes each engine actually expects. Azure wants BCP-47 speech
locales for "from" (e.g. "en-US") and short ISO codes for "to"
(e.g. "fa"). Argos Translate and faster-whisper want plain ISO 639-1
codes on both sides, so we normalize through this one table instead of
scattering string constants across engines.

All three lookup helpers now RAISE on an unknown display name rather
than silently substituting a default. to_azure_target() used to fall
back to Persian while the other two fell back to English, so a typo or
a stale saved language name produced Persian output from an English
source with nothing logged anywhere — a wrong answer is worse than a
loud failure, and pipeline.py already reports engine start errors to
the UI.

Usage:
    from languages import LANGUAGES, to_azure_speech_locale, to_iso639
    LANGUAGES["Persian (Farsi)"]        # -> {"flag": "🇮🇷", "iso": "fa", ...}
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
    "Dutch (Netherlands)": {"flag": "🇳🇱", "iso": "nl", "azure_speech_locale": "nl-NL", "azure_target": "nl"},
}

# The language every offline pair pivots through when no direct Argos
# package exists (see engines/offline_engine.py). Argos publishes its
# packages as a hub-and-spoke set around English, so X -> en -> Y is the
# only route available for most non-English pairs.
PIVOT_ISO = "en"


class UnknownLanguageError(KeyError):
    """
    UnknownLanguageError
    Usage: raised by the to_* helpers when handed a display name that
    isn't a key in LANGUAGES. Subclasses KeyError so existing
    `except KeyError` handlers still catch it.
    """


def _entry(display_name: str) -> dict:
    """
    _entry(display_name)
    Usage: internal — resolves a UI display name to its LANGUAGES row,
    raising UnknownLanguageError with the list of valid names instead of
    returning a silent default.
    """
    try:
        return LANGUAGES[display_name]
    except KeyError:
        raise UnknownLanguageError(
            f"Unknown language display name {display_name!r}. "
            f"Expected one of: {', '.join(sorted(LANGUAGES))}"
        ) from None


def to_azure_speech_locale(display_name: str) -> str:
    """
    to_azure_speech_locale(display_name)
    Usage: pass a UI display name like "English" to get the locale Azure's
    SpeechTranslationConfig expects for `speech_recognition_language`
    (e.g. "en-US"). Raises UnknownLanguageError for unknown names.
    """
    return _entry(display_name)["azure_speech_locale"]


def to_azure_target(display_name: str) -> str:
    """
    to_azure_target(display_name)
    Usage: pass a UI display name to get the short code Azure expects in
    `add_target_language(...)` for the translation output (e.g. "fa",
    "zh-Hans"). Raises UnknownLanguageError for unknown names.
    """
    return _entry(display_name)["azure_target"]


def to_iso639(display_name: str) -> str:
    """
    to_iso639(display_name)
    Usage: pass a UI display name to get the plain ISO 639-1 code used by
    the offline engine (faster-whisper language hint, Argos Translate
    from_code/to_code). Raises UnknownLanguageError for unknown names.
    """
    return _entry(display_name)["iso"]


def all_iso_codes() -> list:
    """
    all_iso_codes()
    Usage: `all_iso_codes()` -> ["en", "fa", "es", ...]. Used by
    scripts/prepare_offline_assets.py so the set of bundled Argos
    packages is derived from this table rather than hand-maintained in a
    second list that can drift out of sync (which is exactly how
    Mandarin and Turkish ended up in the UI with no offline packages).
    """
    return [entry["iso"] for entry in LANGUAGES.values()]
