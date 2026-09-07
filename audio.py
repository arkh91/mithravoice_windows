"""
audio.py

Captures microphone audio directly in Python (via sounddevice) rather
than through the webview's JS, since this is a native desktop window and
sounddevice gives us lower latency plus a real device list regardless of
which OS webview backend (WebView2 / WKWebView / GTK WebKit) is in use.

Usage:
    from audio import list_input_devices, MicrophoneStream

    devices = list_input_devices()               # for the UI's device picker
    with MicrophoneStream(device_index=None, on_chunk=handle_chunk):
        ...                                       # runs until the `with` block exits
"""

import queue
import threading
from typing import Callable, List, Optional, TypedDict

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # required by both Azure Speech SDK and whisper
CHANNELS = 1
BLOCK_SIZE = 1600  # 100ms of audio at 16kHz, a good balance of latency vs. overhead


class DeviceInfo(TypedDict):
    index: int
    name: str
    is_default: bool


def list_input_devices() -> List[DeviceInfo]:
    """
    list_input_devices()
    Usage: call to populate the "Microphone" selector in the settings
    panel. Returns only devices with at least one input channel, each
    flagged with whether it's the OS default so the UI can show
    "Default Device" as it does today.
    """
    devices = sd.query_devices()
    default_index = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    result: List[DeviceInfo] = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            result.append({"index": i, "name": d["name"], "is_default": i == default_index})
    return result


class MicrophoneStream:
    """
    MicrophoneStream(device_index, on_chunk, on_level)
    Usage: use as a context manager (`with MicrophoneStream(...) as mic:`)
    or call .start()/.stop() directly. `on_chunk` is invoked from the
    audio callback thread with each raw int16 mono PCM chunk (BLOCK_SIZE
    samples at SAMPLE_RATE) as soon as it's captured — pass it straight
    into a speech engine's streaming API. `on_level`, if given, is
    invoked with every chunk too, but with a single float 0.0-1.0
    representing that chunk's volume — this drives the UI's waveform
    bars with real audio levels instead of a decorative animation.
    Also exposes a thread-safe `paused` flag so the UI's Pause button
    can stop feeding audio to the engine without tearing down and
    re-opening the OS audio stream.
    """

    def __init__(
        self,
        device_index: Optional[int],
        on_chunk: Callable[[bytes], None],
        on_level: Optional[Callable[[float], None]] = None,
    ):
        self.device_index = device_index
        self.on_chunk = on_chunk
        self.on_level = on_level
        self.paused = threading.Event()
        self._stream: Optional[sd.InputStream] = None
        self._error_queue: "queue.Queue[Exception]" = queue.Queue()

    def _callback(self, indata, frames, time_info, status):
        """
        _callback(indata, frames, time_info, status)
        Usage: internal — passed to sounddevice.InputStream as the audio
        callback. Not called directly. Reports this chunk's volume via
        on_level (0.0 while paused, so the UI shows a flat/quiet meter
        rather than stale movement), then — unless paused — converts the
        buffer to int16 PCM and forwards it to on_chunk.
        """
        if status:
            # Non-fatal glitches (e.g. buffer overrun) surface here; log rather than raise.
            pass

        if self.on_level is not None:
            try:
                if self.paused.is_set():
                    self.on_level(0.0)
                else:
                    samples = indata[:, 0].astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
                    # Typical speech RMS in float32 [-1, 1] is small (quiet
                    # room tone to normal speaking voice rarely exceeds
                    # ~0.15-0.2), so scale up empirically for a meter that
                    # actually moves, then clamp to the 0.0-1.0 the UI expects.
                    self.on_level(min(1.0, rms * 6.0))
            except Exception:
                pass  # the level meter is cosmetic; never let it break audio capture

        if self.paused.is_set():
            return
        pcm16 = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        try:
            self.on_chunk(pcm16)
        except Exception as exc:  # keep the audio thread alive; surface the error to the caller
            self._error_queue.put(exc)

    def start(self) -> None:
        """
        start()
        Usage: opens the OS audio input stream and begins invoking
        on_chunk. Raises immediately if the chosen device can't be opened
        (e.g. unplugged since the device list was fetched).
        """
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            device=self.device_index,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        """
        stop()
        Usage: closes the OS audio stream. Safe to call even if start()
        was never called or the stream is already closed.
        """
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_paused(self, paused: bool) -> None:
        """
        set_paused(paused)
        Usage: bound to the UI's Pause/Resume button. When paused, the
        mic stream stays open (so resuming is instant) but chunks are
        dropped instead of being forwarded to the engine.
        """
        if paused:
            self.paused.set()
        else:
            self.paused.clear()

    def drain_errors(self) -> Optional[Exception]:
        """
        drain_errors()
        Usage: poll periodically from the pipeline's control thread to
        detect exceptions raised inside on_chunk (which runs on the audio
        callback thread and can't otherwise propagate). Returns the next
        queued exception, or None.
        """
        try:
            return self._error_queue.get_nowait()
        except queue.Empty:
            return None

    def __enter__(self) -> "MicrophoneStream":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
