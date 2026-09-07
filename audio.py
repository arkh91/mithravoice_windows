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


def _resolve_input_devices():
    """
    _resolve_input_devices()
    Usage: internal — shared by list_input_devices() and
    MicrophoneStream.start() so the UI's displayed default and the
    device actually recorded from can never disagree. Returns
    (devices, eligible_indices, default_index): devices is
    sd.query_devices()'s raw list, eligible_indices is which of those
    have an input channel, default_index is the OS default among them.

    On Windows, PortAudio enumerates the SAME physical microphone once
    per audio host API (MME, DirectSound, WASAPI, WDM-KS) — so without
    filtering, a single headset can show up as three or four separate,
    confusingly-similar entries. Worse, sd.default.device reflects
    MME's default, which is frequently a generic "Microsoft Sound
    Mapper - Input" proxy device rather than whatever physical device
    Windows' own Sound Settings has actually selected as default.
    WASAPI's default_input_device doesn't have that problem — it
    accurately mirrors the OS-level default — so this prefers WASAPI's
    device list and default when a WASAPI host API is present, falling
    back to the old behavior (any host API, sd.default.device) on
    platforms without one (macOS, Linux) or in the rare case a Windows
    install somehow lacks it.
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    wasapi_index = next(
        (i for i, api in enumerate(hostapis) if "wasapi" in api.get("name", "").lower()),
        None,
    )

    if wasapi_index is not None:
        eligible_indices = [
            i for i, d in enumerate(devices)
            if d.get("max_input_channels", 0) > 0 and d.get("hostapi") == wasapi_index
        ]
        default_index = hostapis[wasapi_index].get("default_input_device", -1)
        # PortAudio uses -1 for "no default set" — fall back to the
        # first eligible WASAPI device rather than showing no default
        # at all in that edge case.
        if default_index not in eligible_indices and eligible_indices:
            default_index = eligible_indices[0]
    else:
        eligible_indices = [i for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
        default_index = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device

    return devices, eligible_indices, default_index


def get_default_input_device_index() -> Optional[int]:
    """
    get_default_input_device_index()
    Usage: called by MicrophoneStream.start() when no explicit device
    was chosen (device_index=None — i.e. the user never touched the
    Settings dropdown), so actual recording resolves to the exact same
    device list_input_devices() marked "is_default": True for in the
    UI. Without this, sd.InputStream(device=None) would let PortAudio
    pick its own default independently, which — per the host-API
    caveat above — is not guaranteed to be the same device, silently
    recording from the wrong microphone despite the UI showing the
    right one. Returns None (letting PortAudio decide) only if no
    input devices could be found at all.
    """
    _, eligible_indices, default_index = _resolve_input_devices()
    if default_index in eligible_indices:
        return default_index
    return eligible_indices[0] if eligible_indices else None


def list_input_devices() -> List[DeviceInfo]:
    """
    list_input_devices()
    Usage: call to populate the "Microphone" selector in the settings
    panel. Returns only devices with at least one input channel, each
    flagged with whether it's the OS default so the UI can show
    "Default Device" as it does today. See _resolve_input_devices()
    above for how "default" is determined.
    """
    devices, eligible_indices, default_index = _resolve_input_devices()
    return [
        {"index": i, "name": devices[i]["name"], "is_default": i == default_index}
        for i in eligible_indices
    ]


def _resample_mono(samples: np.ndarray, orig_sr: float) -> np.ndarray:
    """
    _resample_mono(samples, orig_sr)
    Usage: internal — called from MicrophoneStream._callback() only
    when start() had to open the OS stream at a samplerate other than
    SAMPLE_RATE because the device rejected SAMPLE_RATE outright (see
    start()'s docstring). Azure Speech SDK and faster-whisper both
    require exactly SAMPLE_RATE regardless of what the physical device
    natively captures at, so every chunk is converted here before
    on_chunk ever sees it. Plain linear interpolation via np.interp
    rather than pulling in scipy/resampy as a dependency — this is
    fine for speech-recognition input (no professional audio-quality
    bar to clear) and adds zero new packages to the PyInstaller build.
    """
    if samples.size == 0:
        return samples
    duration = samples.shape[0] / orig_sr
    target_len = max(1, round(duration * SAMPLE_RATE))
    orig_positions = np.linspace(0, duration, num=samples.shape[0], endpoint=False)
    target_positions = np.linspace(0, duration, num=target_len, endpoint=False)
    return np.interp(target_positions, orig_positions, samples).astype(np.float32)


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
        # The channel count / samplerate the OS stream was ACTUALLY
        # opened with (set in start(), read in _callback() to decide
        # whether to downmix/resample). Both equal the ideal
        # CHANNELS/SAMPLE_RATE until start() runs; see start()'s
        # docstring for why a given device might force something else.
        self._open_channels = CHANNELS
        self._open_samplerate: float = SAMPLE_RATE

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

        # Usage: internal — collapses a possibly-multichannel buffer down
        # to a single mono column, then, if the device also refused
        # SAMPLE_RATE itself, resamples up/down to it. Some WASAPI
        # endpoints (notably wireless/USB headset dongles like the
        # SteelSeries Arctis 7P) reject being opened with anything
        # other than their own reported channel count and/or native
        # samplerate — see start()'s docstring for the fallback logic
        # that negotiates a format the device will actually accept.
        # Whatever combination start() ended up with, on_chunk/on_level
        # always still receive plain mono audio at SAMPLE_RATE, exactly
        # as if the device had supported the ideal format directly.
        mono = indata[:, 0] if self._open_channels == 1 else indata.mean(axis=1)
        mono = mono.astype(np.float32)
        if self._open_samplerate != SAMPLE_RATE:
            mono = _resample_mono(mono, self._open_samplerate)

        if self.on_level is not None:
            try:
                if self.paused.is_set():
                    self.on_level(0.0)
                else:
                    rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
                    # Typical speech RMS in float32 [-1, 1] is small (quiet
                    # room tone to normal speaking voice rarely exceeds
                    # ~0.15-0.2), so scale up empirically for a meter that
                    # actually moves, then clamp to the 0.0-1.0 the UI expects.
                    self.on_level(min(1.0, rms * 6.0))
            except Exception:
                pass  # the level meter is cosmetic; never let it break audio capture

        if self.paused.is_set():
            return
        pcm16 = (mono * 32767).astype(np.int16).tobytes()
        try:
            self.on_chunk(pcm16)
        except Exception as exc:  # keep the audio thread alive; surface the error to the caller
            self._error_queue.put(exc)

    def start(self) -> None:
        """
        start()
        Usage: opens the OS audio input stream and begins invoking
        on_chunk. Raises the last PortAudioError encountered if no
        format the device accepts could be found (e.g. unplugged since
        the device list was fetched).

        When device_index is None (the user hasn't explicitly chosen
        one in Settings), resolves to get_default_input_device_index()
        rather than passing device=None straight to sd.InputStream —
        otherwise PortAudio would pick its own default independently,
        which isn't guaranteed to be the same physical device
        list_input_devices() marked "is_default" for the UI (see that
        function's docstring), silently recording from the wrong mic
        despite the UI showing the right one.

        Tries (SAMPLE_RATE, CHANNELS) first, since that's the ideal
        format every normal device accepts and needs no downmixing or
        resampling. Some WASAPI endpoints — seen in practice on
        wireless/USB headset dongles such as the SteelSeries Arctis
        7P — reject that outright: either the channel count
        (PortAudioError -9998, "Invalid number of channels") or the
        samplerate itself (-9997, "Invalid sample rate"), sometimes
        both. Rather than special-casing either error individually,
        this walks a list of candidate (samplerate, channels) pairs
        that each concede one more thing to the device's own reported
        native format, stopping at the first one PortAudio accepts.
        _callback() then downmixes/resamples using self._open_channels
        / self._open_samplerate so on_chunk and on_level always still
        see plain mono audio at SAMPLE_RATE, regardless of which
        format the stream actually had to open in.
        """
        device = self.device_index if self.device_index is not None else get_default_input_device_index()
        device_info = sd.query_devices(device)
        device_channels = max(1, int(device_info.get("max_input_channels", CHANNELS)))
        device_samplerate = float(device_info.get("default_samplerate", SAMPLE_RATE)) or SAMPLE_RATE

        candidates = [
            (SAMPLE_RATE, CHANNELS),
            (SAMPLE_RATE, device_channels),
            (device_samplerate, CHANNELS),
            (device_samplerate, device_channels),
        ]
        # De-duplicate while preserving preference order (e.g. when
        # device_channels == CHANNELS, several entries above collapse
        # to the same pair — no need to try opening it twice).
        seen: set = set()
        unique_candidates = [c for c in candidates if not (c in seen or seen.add(c))]

        last_exc: Optional[Exception] = None
        for samplerate, channels in unique_candidates:
            # Scale blocksize to whatever samplerate we're attempting so
            # every candidate still delivers ~100ms chunks (BLOCK_SIZE's
            # original intent) rather than silently drifting shorter or
            # longer as the samplerate changes across candidates.
            blocksize = max(1, round(BLOCK_SIZE * samplerate / SAMPLE_RATE))
            try:
                self._stream = sd.InputStream(
                    samplerate=samplerate,
                    channels=channels,
                    dtype="float32",
                    blocksize=blocksize,
                    device=device,
                    callback=self._callback,
                )
            except sd.PortAudioError as exc:
                last_exc = exc
                continue
            self._open_channels = channels
            self._open_samplerate = samplerate
            self._stream.start()
            return

        raise last_exc

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
