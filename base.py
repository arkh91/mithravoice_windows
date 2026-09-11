"""
engines/base.py

The common interface every translation engine implements, so
pipeline.py can swap between the online (Azure) and offline
(Whisper + Argos) engines — including switching mid-session on
failure — without knowing which one it's talking to.

Usage:
    class MyEngine(SpeechTranslator):
        def start(self, from_lang, to_lang, on_result): ...
        def feed(self, pcm16_chunk): ...
        def stop(self): ...
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class TranslationResult:
    original_text: str
    translated_text: str
    is_final: bool  # False = interim/partial result still being refined, True = settled utterance


# Callback signature every engine invokes with each recognized/translated chunk.
ResultCallback = Callable[[TranslationResult], None]

# Callback signature an engine invokes when it fails asynchronously —
# i.e. NOT from a call the pipeline made, but from the engine's own
# background thread (a dropped connection, an auth rejection, a service
# outage). Receives the exception.
ErrorCallback = Callable[[Exception], None]


class SpeechTranslator(ABC):
    """
    SpeechTranslator
    Usage: base class for translation engines. Call start() once per
    session with the UI's selected languages and a callback, stream mic
    audio in with repeated feed() calls, and call stop() when the user
    pauses or the app closes. Implementations must be safe to stop() even
    if start() failed partway through.

    Set `on_error` before start() to be told about failures the engine
    discovers on its OWN threads. This exists because the obvious
    alternative does not work: an engine that raises inside an SDK
    callback is raising on a thread the SDK owns, where nothing is
    waiting to catch it. The exception is swallowed (or takes down only
    that callback thread) and the pipeline never learns anything is
    wrong — it just stops receiving results, which is indistinguishable
    from nobody talking. That is precisely how a dropped internet
    connection used to leave the app silently dead instead of falling
    back to the offline engine.
    """

    # Default no-op so an engine can always call self.on_error without
    # checking, and so an engine used without a pipeline still works.
    on_error: ErrorCallback = staticmethod(lambda exc: None)

    def _report_error(self, exc: Exception) -> None:
        """
        _report_error(exc)
        Usage: called BY AN ENGINE from its own background thread when
        it has failed and cannot recover on its own. Never raises, so a
        broken error handler can't take down the SDK thread it is
        running on — which would turn a recoverable network drop into a
        dead session.
        """
        try:
            self.on_error(exc)
        except Exception as inner:  # noqa: BLE001 - reporting a failure must not itself fail
            print(f"[engine] on_error handler raised: {inner!r}", flush=True)

    @abstractmethod
    def start(self, from_lang: str, to_lang: str, on_result: ResultCallback) -> None:
        """
        start(from_lang, to_lang, on_result)
        Usage: from_lang/to_lang are UI display names (e.g. "English",
        "Persian (Farsi)") — implementations translate these via
        languages.py into whatever locale format they need. on_result is
        invoked (possibly from a background thread) for every interim and
        final recognition/translation.
        """
        raise NotImplementedError

    @abstractmethod
    def feed(self, pcm16_chunk: bytes) -> None:
        """
        feed(pcm16_chunk)
        Usage: called once per audio chunk from MicrophoneStream (16kHz,
        mono, 16-bit PCM bytes). Must be safe to call at ~10Hz without
        blocking the audio callback thread for long.
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """
        stop()
        Usage: ends the current recognition session and releases any
        engine-specific resources (network connection, loaded buffers).
        Safe to call multiple times.
        """
        raise NotImplementedError
