"""
pipeline.py

Wires MicrophoneStream to a translation engine and pushes results back
out through a single callback. Owns the online/offline fallback logic:
tries AzureEngine first (unless the user forced offline mode), and if it
fails to start or raises mid-session, tears it down and restarts on
OfflineEngine transparently — the UI only ever sees TranslationResults
and, optionally, an engine-mode change notification.

Usage:
    pipeline = TranslationPipeline(on_result=push_to_ui, on_engine_change=notify_ui, on_level=push_level_to_ui)
    pipeline.start(from_lang="English", to_lang="Persian (Farsi)", device_index=None, mode="auto")
    pipeline.set_paused(True)
    pipeline.stop()
"""

import threading
import time
from typing import Callable, Literal, Optional

from audio import MicrophoneStream
from engines.azure_engine import AzureEngine, AzureEngineError
from engines.base import ResultCallback, SpeechTranslator, TranslationResult
from engines.offline_engine import OfflineEngine, OfflineEngineError

EngineMode = Literal["auto", "online", "offline"]
EngineChangeCallback = Callable[[str], None]  # receives "online" | "offline"
LevelCallback = Callable[[float], None]  # receives 0.0-1.0 per audio chunk


class TranslationPipeline:
    """
    TranslationPipeline
    Usage: one instance lives for the app's lifetime (see main.py's Api).
    Call start() when the user hits the mic button, set_paused() for the
    Pause button, and stop() when they mute or the window closes.
    """

    def __init__(
        self,
        on_result: ResultCallback,
        on_engine_change: Optional[EngineChangeCallback] = None,
        on_level: Optional[LevelCallback] = None,
    ):
        self._on_result = on_result
        self._on_engine_change = on_engine_change
        self._on_level = on_level
        self._mic: Optional[MicrophoneStream] = None
        self._engine: Optional[SpeechTranslator] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_running = threading.Event()
        self._from_lang = "English"
        self._to_lang = "Persian (Farsi)"
        self._device_index: Optional[int] = None
        self._mode: EngineMode = "auto"
        self._lock = threading.Lock()

    def _make_engine(self, prefer_online: bool) -> SpeechTranslator:
        """
        _make_engine(prefer_online)
        Usage: internal — returns a fresh, unstarted engine instance
        based on prefer_online and the pipeline's configured mode.
        """
        if self._mode == "offline" or not prefer_online:
            return OfflineEngine()
        return AzureEngine()

    def _start_engine(self, prefer_online: bool) -> None:
        """
        _start_engine(prefer_online)
        Usage: internal — instantiates and starts an engine, falling back
        to OfflineEngine once if the online attempt fails (missing
        credentials, auth error, network error). Notifies on_engine_change
        so the UI can reflect which engine ended up active.
        """
        engine = self._make_engine(prefer_online)
        active_mode = "offline" if isinstance(engine, OfflineEngine) else "online"
        try:
            engine.start(self._from_lang, self._to_lang, self._on_engine_result)
        except (AzureEngineError, Exception) as exc:  # noqa: BLE001 - any online failure triggers fallback
            if active_mode == "online" and self._mode == "auto":
                # Print the real reason Azure failed BEFORE silently
                # switching engines — without this, a fallback to
                # offline is completely invisible in the console, and
                # the only symptom is unexpectedly worse translation
                # quality with no clue why (see the "(Offline)" badge
                # in the UI for the same signal, less detail).
                print(f"[pipeline] Azure engine failed to start, falling back to offline: {exc!r}")
                engine = OfflineEngine()
                active_mode = "offline"
                engine.start(self._from_lang, self._to_lang, self._on_engine_result)
            else:
                raise

        self._engine = engine
        if self._on_engine_change:
            self._on_engine_change(active_mode)

    def _on_engine_result(self, result: TranslationResult) -> None:
        """
        _on_engine_result(result)
        Usage: internal — the callback handed to whichever engine is
        active. Just forwards to the pipeline's own on_result so callers
        don't need to know which engine produced it.
        """
        self._on_result(result)

    def _watchdog_loop(self) -> None:
        """
        _watchdog_loop()
        Usage: internal — runs on a background thread while a session is
        active. Polls the mic stream for exceptions raised inside its
        audio callback (e.g. an AzureEngineError raised from feed()) and,
        in "auto" mode, restarts the session on the offline engine if the
        online engine dies mid-stream.
        """
        while self._watchdog_running.is_set():
            time.sleep(0.5)
            if self._mic is None:
                continue
            error = self._mic.drain_errors()
            if error is None:
                continue
            # Same visibility gap as _start_engine(): a mid-session
            # drop to offline is otherwise silent in the console.
            print(f"[pipeline] mic feed error while online, falling back to offline: {error!r}")
            if self._mode == "auto" and isinstance(self._engine, AzureEngine):
                self._restart_on_offline()

    def _restart_on_offline(self) -> None:
        """
        _restart_on_offline()
        Usage: internal — swaps the active engine to OfflineEngine
        without dropping the mic stream, so a lost internet connection
        mid-session degrades gracefully instead of going silent.
        """
        with self._lock:
            if self._engine is not None:
                self._engine.stop()
            self._engine = OfflineEngine()
            self._engine.start(self._from_lang, self._to_lang, self._on_engine_result)
            if self._on_engine_change:
                self._on_engine_change("offline")

    def start(self, from_lang: str, to_lang: str, device_index: Optional[int], mode: EngineMode = "auto") -> None:
        """
        start(from_lang, to_lang, device_index, mode)
        Usage: begin a live translation session. from_lang/to_lang are UI
        display names, device_index is a value from audio.list_input_devices()
        (or None for the system default), mode forces "online" or
        "offline" or leaves it as "auto" (online with automatic fallback).
        """
        with self._lock:
            self._from_lang, self._to_lang = from_lang, to_lang
            self._device_index, self._mode = device_index, mode

            self._start_engine(prefer_online=(mode != "offline"))

            self._mic = MicrophoneStream(device_index=device_index, on_chunk=self._feed_engine, on_level=self._on_level)
            self._mic.start()

            self._watchdog_running.set()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
            self._watchdog_thread.start()

    def _feed_engine(self, pcm16_chunk: bytes) -> None:
        """
        _feed_engine(pcm16_chunk)
        Usage: internal — the callback given to MicrophoneStream. Forwards
        each captured chunk to whichever engine is currently active.
        """
        if self._engine is not None:
            self._engine.feed(pcm16_chunk)

    def set_paused(self, paused: bool) -> None:
        """
        set_paused(paused)
        Usage: bound to the UI's Pause/Resume button. Leaves the mic
        stream and engine connection open but stops audio from being
        forwarded, so resuming is instant.
        """
        if self._mic is not None:
            self._mic.set_paused(paused)

    @property
    def is_online_active(self) -> bool:
        """
        is_online_active
        Usage: `if pipeline.is_online_active: ...` — read-only check of
        whether the currently active engine is AzureEngine (as opposed
        to OfflineEngine, or no engine at all because no session is
        running). api.py's set_paused() uses this to decide whether
        resuming from pause should restart the online-usage clock
        (see usage.py) — pausing/resuming must never start that clock
        for an offline-only session.
        """
        return isinstance(self._engine, AzureEngine)

    def stop(self) -> None:
        """
        stop()
        Usage: ends the session entirely — closes the mic stream, stops
        the active engine, and shuts down the watchdog thread. Safe to
        call even if start() was never called.
        """
        with self._lock:
            self._watchdog_running.clear()
            if self._mic is not None:
                self._mic.stop()
                self._mic = None
            if self._engine is not None:
                self._engine.stop()
                self._engine = None
