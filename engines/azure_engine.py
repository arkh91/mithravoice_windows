"""
engines/azure_engine.py

Online translation engine using Azure Cognitive Services Speech
Translation. Does streaming speech recognition and translation in one
API call, which is why it's the primary engine — no separate
STT-then-translate round trip.

Requires AZURE_SPEECH_KEY and AZURE_SPEECH_REGION (see config.py / .env).

Usage:
    engine = AzureEngine()
    engine.start("English", "Persian (Farsi)", on_result=my_callback)
    engine.feed(pcm16_bytes)   # call repeatedly as audio arrives
    engine.stop()
"""

import azure.cognitiveservices.speech as speechsdk

from config import settings
from languages import to_azure_speech_locale, to_azure_target

from .base import ResultCallback, SpeechTranslator, TranslationResult


class AzureEngineError(RuntimeError):
    """Raised when Azure can't be reached or credentials are missing/invalid."""


class AzureEngine(SpeechTranslator):
    """
    AzureEngine
    Usage: see module docstring. Internally pushes fed audio into a
    PushAudioInputStream that the Speech SDK reads from, and forwards
    every `recognizing` (interim) and `recognized` (final) event to the
    on_result callback supplied to start().
    """

    def __init__(self) -> None:
        self._recognizer: speechsdk.translation.TranslationRecognizer | None = None
        self._push_stream: speechsdk.audio.PushAudioInputStream | None = None
        self._to_lang_display: str = ""

    def start(self, from_lang: str, to_lang: str, on_result: ResultCallback) -> None:
        """
        start(from_lang, to_lang, on_result)
        Usage: see SpeechTranslator.start. Raises AzureEngineError if
        AZURE_SPEECH_KEY/REGION aren't configured — the pipeline should
        catch this and fall back to the offline engine.
        """
        if not settings.azure_configured:
            raise AzureEngineError("Azure Speech key/region not configured")

        self._to_lang_display = to_lang

        speech_config = speechsdk.translation.SpeechTranslationConfig(
            subscription=settings.azure_speech_key,
            region=settings.azure_speech_region,
        )
        speech_config.speech_recognition_language = to_azure_speech_locale(from_lang)
        speech_config.add_target_language(to_azure_target(to_lang))

        # 16kHz mono 16-bit PCM matches audio.py's MicrophoneStream output exactly.
        stream_format = speechsdk.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16, channels=1)
        self._push_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self._push_stream)

        self._recognizer = speechsdk.translation.TranslationRecognizer(
            translation_config=speech_config, audio_config=audio_config
        )

        target_code = to_azure_target(to_lang)

        def _on_recognizing(evt: speechsdk.translation.TranslationRecognitionEventArgs) -> None:
            """Interim (still-being-refined) result — forwarded with is_final=False."""
            translated = evt.result.translations.get(target_code, "")
            if evt.result.text:
                on_result(TranslationResult(evt.result.text, translated, is_final=False))

        def _on_recognized(evt: speechsdk.translation.TranslationRecognitionEventArgs) -> None:
            """Settled utterance — forwarded with is_final=True."""
            translated = evt.result.translations.get(target_code, "")
            if evt.result.text:
                on_result(TranslationResult(evt.result.text, translated, is_final=True))

        def _on_canceled(evt: speechsdk.translation.TranslationRecognitionCanceledEventArgs) -> None:
            """Connection dropped / auth failed mid-session — surfaced as an exception."""
            raise AzureEngineError(f"Azure recognition canceled: {evt.reason} — {evt.error_details}")

        self._recognizer.recognizing.connect(_on_recognizing)
        self._recognizer.recognized.connect(_on_recognized)
        self._recognizer.canceled.connect(_on_canceled)
        self._recognizer.start_continuous_recognition()

    def feed(self, pcm16_chunk: bytes) -> None:
        """
        feed(pcm16_chunk)
        Usage: see SpeechTranslator.feed. Writes straight into the Speech
        SDK's push stream; a no-op if start() hasn't been called yet.
        """
        if self._push_stream is not None:
            self._push_stream.write(pcm16_chunk)

    def stop(self) -> None:
        """
        stop()
        Usage: see SpeechTranslator.stop. Stops continuous recognition and
        closes the push stream so the SDK's background threads exit.
        """
        if self._recognizer is not None:
            self._recognizer.stop_continuous_recognition()
            self._recognizer = None
        if self._push_stream is not None:
            self._push_stream.close()
            self._push_stream = None
