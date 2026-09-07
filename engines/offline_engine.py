"""
engines/offline_engine.py

Offline fallback engine — no network required. Speech-to-text via
faster-whisper (runs fine on CPU), text translation via Argos Translate
(fully local, open-source MT). Used automatically by pipeline.py
whenever the Azure engine is unavailable (no credentials, or the
connection drops mid-session).

Whisper isn't a streaming API, so this engine buffers incoming audio and
transcribes it once per utterance: a background thread checks every
FLUSH_INTERVAL seconds whether the buffer is "final" — a quiet trailing
chunk (simple energy-based VAD) or hitting MAX_BUFFER_SECONDS — and only
THEN runs one Whisper call on the whole segment. It deliberately does
NOT transcribe on every tick while a segment is still in progress:
doing so would mean repeatedly re-transcribing a growing, overlapping
buffer several times before the segment finalizes, which on hardware
slower than realtime for the chosen model compounds into growing,
snowballing latency. One transcription per finalized utterance keeps
latency bounded to roughly "how long Whisper takes on one short clip,"
at the cost of no live partial captions mid-utterance.

Usage:
    engine = OfflineEngine()
    engine.start("English", "Persian (Farsi)", on_result=my_callback)
    engine.feed(pcm16_bytes)
    engine.stop()
"""

import threading
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from languages import to_iso639

from .base import ResultCallback, SpeechTranslator, TranslationResult

SAMPLE_RATE = 16000
FLUSH_INTERVAL = 0.6  # seconds between silence/finality checks (cheap — no transcription happens on most ticks)
MAX_BUFFER_SECONDS = 6.0  # force a final flush so a long run-on sentence still gets transcribed periodically
SILENCE_RMS_THRESHOLD = 250  # int16 RMS below this on the trailing 300ms counts as "quiet"


class OfflineEngineError(RuntimeError):
    """Raised when required offline models/packages can't be loaded."""


class OfflineEngine(SpeechTranslator):
    """
    OfflineEngine
    Usage: see module docstring. Lazily loads the whisper model and Argos
    translation package on first start() so the app launches instantly
    even if this engine is never needed (Azure is healthy).
    """

    def __init__(self) -> None:
        self._model = None  # faster_whisper.WhisperModel, loaded lazily
        self._buffer: List[bytes] = []
        self._buffer_lock = threading.Lock()
        self._buffer_started_at: Optional[float] = None
        self._running = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._from_iso = "en"
        self._to_iso = "fa"
        self._on_result: Optional[ResultCallback] = None

    def _load_whisper(self):
        """
        _load_whisper()
        Usage: internal — imports and instantiates faster_whisper.WhisperModel
        on first use. Prefers a bundled, pre-downloaded ctranslate2 model
        directory (settings.whisper_model_path, populated by
        scripts/prepare_offline_assets.py before packaging) so the
        installed app needs zero network access. Falls back to
        downloading by model size — convenient for `python main.py`
        during development, but NOT what ships in the Windows build.
        """
        from faster_whisper import WhisperModel

        from config import settings

        bundled_path = Path(settings.whisper_model_path)
        if bundled_path.exists():
            return WhisperModel(str(bundled_path), device=settings.whisper_device, compute_type=settings.whisper_compute_type)

        return WhisperModel(
            settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )

    def _ensure_argos_language_pair(self, from_iso: str, to_iso: str) -> None:
        """
        _ensure_argos_language_pair(from_iso, to_iso)
        Usage: internal — called once in start(). If the pair is already
        installed, does nothing. Otherwise looks for a matching
        .argosmodel file bundled in settings.argos_packages_dir (put
        there by scripts/prepare_offline_assets.py) and installs it
        locally with no network access. Only if neither is available
        does it fall back to downloading from the Argos package index —
        which requires internet and is a dev-only convenience, not
        something the shipped installer should ever need to do.
        """
        import argostranslate.package
        import argostranslate.translate

        from config import settings

        installed = argostranslate.translate.get_installed_languages()
        have_pair = any(
            lang.code == from_iso and lang.get_translation(next(l for l in installed if l.code == to_iso))
            for lang in installed
            if any(l.code == to_iso for l in installed)
        )
        if have_pair:
            return

        bundled_dir = Path(settings.argos_packages_dir)
        expected_name_fragment = f"{from_iso}_{to_iso}"
        if bundled_dir.exists():
            for candidate in bundled_dir.glob("*.argosmodel"):
                if expected_name_fragment in candidate.name:
                    argostranslate.package.install_from_path(str(candidate))
                    return

        # Dev-only fallback: no bundled package found, try the network.
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
        match = next((p for p in available if p.from_code == from_iso and p.to_code == to_iso), None)
        if match is None:
            raise OfflineEngineError(f"No Argos Translate package available for {from_iso} -> {to_iso}")
        argostranslate.package.install_from_path(match.download())

    def start(self, from_lang: str, to_lang: str, on_result: ResultCallback) -> None:
        """
        start(from_lang, to_lang, on_result)
        Usage: see SpeechTranslator.start. Loads the whisper model and
        makes sure the Argos language pair is installed (may briefly hit
        the network the very first time a pair is used), then starts the
        background flush thread that drives transcription.
        """
        self._from_iso = to_iso639(from_lang)
        self._to_iso = to_iso639(to_lang)
        self._on_result = on_result

        if self._model is None:
            self._model = self._load_whisper()
        self._ensure_argos_language_pair(self._from_iso, self._to_iso)

        self._running.set()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def feed(self, pcm16_chunk: bytes) -> None:
        """
        feed(pcm16_chunk)
        Usage: see SpeechTranslator.feed. Appends the chunk to the rolling
        buffer; the background flush thread does the actual transcription
        work, so this call is cheap and safe from the audio thread.
        """
        with self._buffer_lock:
            if self._buffer_started_at is None:
                self._buffer_started_at = time.monotonic()
            self._buffer.append(pcm16_chunk)

    def _is_trailing_silence(self, chunks: List[bytes]) -> bool:
        """
        _is_trailing_silence(chunks)
        Usage: internal — crude voice-activity check used to decide
        whether a flush should be marked "final". Looks only at the last
        ~300ms of buffered audio and compares its RMS energy to
        SILENCE_RMS_THRESHOLD.
        """
        if not chunks:
            return True
        tail = b"".join(chunks[-3:])  # ~300ms at 100ms/chunk
        samples = np.frombuffer(tail, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return True
        rms = float(np.sqrt(np.mean(samples**2)))
        return rms < SILENCE_RMS_THRESHOLD

    def _flush_loop(self) -> None:
        """
        _flush_loop()
        Usage: internal — runs on a background thread for the lifetime of
        the session. Every FLUSH_INTERVAL seconds, checks whether the
        buffered audio is ready to finalize (silence detected, or the
        MAX_BUFFER_SECONDS cap hit) and, if so, transcribes it exactly
        once and clears the buffer. If not yet final, it does NOT
        transcribe — it just keeps accumulating and checks again next
        tick. This is deliberate: transcribing on every tick regardless
        of finality means re-transcribing a growing, overlapping buffer
        repeatedly (2-3x redundant Whisper calls per utterance), which
        on hardware slower than realtime for the model snowballs into
        growing, compounding latency. One transcription per finalized
        utterance instead of one per tick keeps latency bounded to
        roughly "however long Whisper takes on one ~3-8s clip", not a
        growing backlog. The tradeoff: no live partial captions while
        still speaking — the subtitle updates once per pause, not
        continuously — which in practice is far more legible than
        partial output.
        """
        while self._running.is_set():
            time.sleep(FLUSH_INTERVAL)
            with self._buffer_lock:
                if not self._buffer:
                    continue
                chunks = list(self._buffer)
                buffer_age = time.monotonic() - (self._buffer_started_at or time.monotonic())
                is_final = self._is_trailing_silence(chunks) or buffer_age >= MAX_BUFFER_SECONDS
                if not is_final:
                    continue  # keep accumulating; don't transcribe an unfinished utterance yet
                self._buffer.clear()
                self._buffer_started_at = None

            self._transcribe_and_translate(chunks, is_final=True)

    def _transcribe_and_translate(self, chunks: List[bytes], is_final: bool) -> None:
        """
        _transcribe_and_translate(chunks, is_final)
        Usage: internal — runs whisper on the given PCM chunks, translates
        the resulting text with Argos, and calls on_result. Silently
        skips empty transcriptions (e.g. pure silence) rather than
        emitting a blank subtitle.
        """
        import argostranslate.translate

        audio = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language=self._from_iso, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text:
            return

        translated = argostranslate.translate.translate(text, self._from_iso, self._to_iso)
        if self._on_result:
            self._on_result(TranslationResult(text, translated, is_final=is_final))

    def stop(self) -> None:
        """
        stop()
        Usage: see SpeechTranslator.stop. Stops the flush thread and
        drops any unflushed buffered audio (a paused/stopped session
        shouldn't emit a stale subtitle later).
        """
        self._running.clear()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=FLUSH_INTERVAL + 1)
            self._flush_thread = None
        with self._buffer_lock:
            self._buffer.clear()
            self._buffer_started_at = None
