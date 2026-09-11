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

import platform
import queue
import threading
import time
from typing import Callable, List, Optional, TypedDict

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # required by both Azure Speech SDK and whisper
CHANNELS = 1
BLOCK_SIZE = 1600  # 100ms of audio at 16kHz, a good balance of latency vs. overhead

# Guards the PortAudio library itself, not any one stream. refresh_devices()
# tears the whole library down and brings it back up, which invalidates every
# open stream in the process — so it may only run when nothing is recording.
# _open_stream_count is how that is known: MicrophoneStream bumps it in
# start() and drops it in stop().
#
# CRITICAL: every call into `sd` that reads PortAudio state — query_devices(),
# query_hostapis(), sd.default.device — must ALSO hold this lock, not just the
# refresh that mutates it. Between sd._terminate() and sd._initialize() the
# library genuinely has no state, and any concurrent reader lands in that
# window and gets `PortAudio not initialized [PaErrorCode -10000]` or
# `Error querying host API N`. That is not a hardware fault and no amount of
# per-device tolerance can absorb it; it is two threads using one global C
# library at once. The real-world version: the watchdog calls
# refresh_devices() from TranslationPipeline._swap_microphone() at the same
# moment the UI's device poll calls list_input_devices(refresh=True) on the
# pywebview bridge thread. RLock (not Lock) because refresh_devices() is
# called from inside list_input_devices(), which now holds the lock itself.
_portaudio_lock = threading.RLock()
_open_stream_count = 0


def refresh_devices() -> bool:
    """
    refresh_devices()
    Usage: call before re-reading the device list when you need to see
    hardware that was plugged in (or unplugged) since the app started —
    e.g. from list_input_devices(refresh=True), or right after closing
    the mic stream in order to reopen it on a newly-selected default.
    Returns True if the list was actually refreshed, False if it was
    skipped because a stream is currently open.

    This exists because PortAudio enumerates audio devices ONCE, at
    Pa_Initialize() time, and caches the result for the lifetime of the
    library. sd.query_devices() re-reads that cache, not the OS — so
    without this, plugging in a headset (or making a different mic the
    Windows default) is completely invisible to the app until it is
    restarted, no matter how often the device list is polled. That was
    the root cause of both the settings/status labels showing a stale
    microphone name and the session continuing to record from a device
    the user had already switched away from.

    The only way to make PortAudio re-enumerate is to terminate and
    re-initialize it, which also silently aborts every open stream —
    hence the _open_stream_count guard rather than just calling it
    whenever. Callers that DO want to refresh mid-session must stop
    their stream first (see TranslationPipeline._swap_microphone).
    """
    global _open_stream_count
    with _portaudio_lock:
        if _open_stream_count > 0:
            return False
        try:
            sd._terminate()
            sd._initialize()
            return True
        except Exception as exc:  # noqa: BLE001 - a failed refresh must never break capture
            print(f"[audio] device refresh failed: {exc!r}", flush=True)
            return False


# --- Windows default-endpoint detection -------------------------------------
#
# COM identifiers for the Core Audio device enumerator. Written as strings and
# parsed with CLSIDFromString rather than hand-packing the byte layout, which
# is easy to get subtly wrong and fails silently when you do.
_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_E_DATA_FLOW_CAPTURE = 1  # eCapture
_E_ROLE_CONSOLE = 0  # eConsole — the "Default Device" in Windows Sound settings

# Remembers whether the COM path has already failed once, so a machine where
# it doesn't work logs a single line instead of one per poll.
_endpoint_probe_failed = False


def get_default_input_endpoint_id() -> Optional[str]:
    """
    get_default_input_endpoint_id()
    Usage: poll every second or two while a session is running and
    compare the result to the previous call — when the string changes,
    Windows' default recording device changed and the mic stream needs
    to be reopened (see TranslationPipeline._watchdog_loop). Returns an
    opaque endpoint ID string, or None on non-Windows or if Core Audio
    couldn't be reached.

    Deliberately does NOT go through PortAudio. PortAudio's idea of the
    default device is frozen at Pa_Initialize() time (see
    refresh_devices above), so asking it "did the default change?" can
    only ever answer no. This asks Windows directly, through the same
    Core Audio enumerator the Sound settings panel itself uses, and is
    cheap enough to poll and — crucially — safe to call while a stream
    is open, which refresh_devices() is not.

    The ID is used only for CHANGE DETECTION; the human-readable name
    and the index to record from still come from PortAudio afterwards,
    once the stream has been closed and the list refreshed.
    """
    global _endpoint_probe_failed
    if platform.system() != "Windows" or _endpoint_probe_failed:
        return None

    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _method(interface_ptr, index, argtypes):
        """
        _method(interface_ptr, index, argtypes)
        Usage: internal — builds a callable for the COM vtable slot at
        `index` on `interface_ptr`. A COM interface pointer points at a
        pointer to an array of function pointers, so this dereferences
        twice. Slots 0-2 are always IUnknown's QueryInterface/AddRef/
        Release; the interface's own methods start at 3, in the order
        they are declared in its IDL.
        """
        vtable = ctypes.cast(interface_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        func_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        return proto(func_ptr)

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long

    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    device_id = ctypes.c_wchar_p()
    initialized = False
    try:
        # COINIT_MULTITHREADED (0). S_OK (0) and S_FALSE (1) both mean this
        # call has to be balanced by a CoUninitialize; RPC_E_CHANGED_MODE
        # means the thread was already initialized as STA by something else,
        # which is fine to use and must NOT be uninitialized here.
        hr = ole32.CoInitializeEx(None, 0)
        initialized = hr in (0, 1)

        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(_CLSID_MMDeviceEnumerator), ctypes.byref(clsid))
        ole32.CLSIDFromString(ctypes.c_wchar_p(_IID_IMMDeviceEnumerator), ctypes.byref(iid))

        CLSCTX_ALL = 23
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL,
                                  ctypes.byref(iid), ctypes.byref(enumerator)) != 0:
            return None

        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(dataFlow, role, **device)
        get_default = _method(enumerator, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])
        if get_default(enumerator, _E_DATA_FLOW_CAPTURE, _E_ROLE_CONSOLE, ctypes.byref(device)) != 0:
            return None  # no recording device at all right now

        # IMMDevice::GetId(**id) — the string is allocated by the callee and
        # has to be handed back with CoTaskMemFree.
        get_id = _method(device, 5, [ctypes.POINTER(ctypes.c_wchar_p)])
        if get_id(device, ctypes.byref(device_id)) != 0:
            return None
        value = device_id.value
        ole32.CoTaskMemFree(device_id)
        return value
    except Exception as exc:  # noqa: BLE001 - detection is an optimisation, never a requirement
        _endpoint_probe_failed = True
        print(f"[audio] default-endpoint detection unavailable, falling back to "
              f"stream-health checks only: {exc!r}", flush=True)
        return None
    finally:
        for handle in (device, enumerator):
            if handle:
                try:
                    _method(handle, 2, [])(handle)  # IUnknown::Release
                except Exception:
                    pass
        if initialized:
            ole32.CoUninitialize()


class DeviceInfo(TypedDict):
    index: int
    name: str
    is_default: bool


def _query_devices_tolerantly() -> list:
    """
    _query_devices_tolerantly()
    Usage: internal — like sd.query_devices(), but survives a single
    device that PortAudio can't describe.

    sd.query_devices() with no arguments builds the whole list in one
    generator expression, so ONE bad device takes the entire call down
    with `PortAudioError: Error querying device 10`. That happens more
    often than it sounds: a disconnected Bluetooth headset, a virtual
    cable left behind by uninstalled software, or a device being
    re-enumerated at the exact moment of the query. The result was an
    unhandled exception thrown across the pywebview bridge from
    list_microphones(), which polls on a timer — so one stale ghost
    device meant no microphone list at all, over and over.

    The device count comes from query_hostapis(), which reports the
    indices each host API owns. That bound matters: querying by index
    until it fails can't work, because PortAudio raises the SAME
    "Error querying device N" for an index past the end as it does for
    a broken one, so a loop that treats the error as "skip and
    continue" never terminates.

    Unreadable devices keep their slot as a placeholder with zero input
    channels, so they're filtered out as ineligible while every later
    device keeps its true PortAudio index — which is what
    MicrophoneStream.start() hands back to sd.InputStream.

    Runs entirely under _portaudio_lock. Without that, a concurrent
    refresh_devices() could terminate the library halfway through this
    function and turn a per-device tolerance problem into a
    "PortAudio not initialized" one, which no per-device handling can
    recover from.
    """
    with _portaudio_lock:
        try:
            return list(sd.query_devices())
        except Exception as exc:  # noqa: BLE001 - one bad device; fall back to per-device querying
            print(f"[audio] bulk device query failed ({exc!r}); querying devices individually", flush=True)

        try:
            indices = {i for api in sd.query_hostapis() for i in api.get("devices", [])}
        except Exception as exc:  # noqa: BLE001 - without a bound there's nothing safe to iterate
            print(f"[audio] could not enumerate host APIs: {exc!r}", flush=True)
            return []

        if not indices:
            return []

        devices = [{"name": f"<unavailable device {i}>", "max_input_channels": 0, "max_output_channels": 0, "hostapi": -1}
                   for i in range(max(indices) + 1)]
        for i in sorted(indices):
            try:
                devices[i] = sd.query_devices(i)
            except Exception as exc:  # noqa: BLE001 - skip exactly the devices that can't be described
                print(f"[audio] skipping unreadable device {i}: {exc}", flush=True)
        return devices


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
    Two things here used to be able to throw straight out through the
    pywebview bridge, taking list_microphones() and — worse —
    MicrophoneStream.start() down with them:

      * the whole function ran outside _portaudio_lock, so a concurrent
        refresh_devices() could pull PortAudio out from under it (see
        that lock's comment for the exact thread pairing);
      * sd.query_hostapis() was called bare, while the device query on
        the line above it was carefully wrapped. It is the same call,
        against the same library, with the same failure modes — both
        `Error querying host API 1` and `PortAudio not initialized`
        came from this one line. A missing host API list is not fatal:
        it only means the WASAPI preference can't be applied, so fall
        through to the generic any-host-API path instead of failing the
        whole session over it.
    """
    with _portaudio_lock:
        devices = _query_devices_tolerantly()

        try:
            hostapis = sd.query_hostapis()
        except Exception as exc:  # noqa: BLE001 - degrade to the non-WASAPI path, never kill the caller
            print(f"[audio] could not read host APIs ({exc!r}); falling back to the plain device list", flush=True)
            hostapis = []

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
            try:
                default = sd.default.device
                default_index = default[0] if isinstance(default, (list, tuple)) else default
            except Exception as exc:  # noqa: BLE001 - sd.default reads PortAudio state too
                print(f"[audio] could not read the PortAudio default device ({exc!r})", flush=True)
                default_index = eligible_indices[0] if eligible_indices else -1

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


def list_input_devices(refresh: bool = False) -> List[DeviceInfo]:
    """
    list_input_devices(refresh=False)
    Usage: call to populate the "Microphone" display in the settings
    panel and the status line on the Live Translation page. Returns
    only devices with at least one input channel, each flagged with
    whether it's the OS default so the UI can show "Default Device" as
    it does today. See _resolve_input_devices() above for how
    "default" is determined.

    Pass refresh=True from anything that polls this on a timer. Without
    it, PortAudio answers from the device list it cached at startup, so
    a headset plugged in five minutes ago simply isn't in the returned
    list and a headset unplugged five minutes ago still is — see
    refresh_devices() for why. The refresh is skipped automatically (no
    error, just slightly stale data for that one call) while a stream
    is open, since it would abort the recording.
    """
    # Refresh and read as one unit. Both halves take _portaudio_lock
    # individually, but taking it once around the pair is what stops a
    # second thread refreshing in the gap between them and handing this
    # caller indices that no longer mean anything.
    with _portaudio_lock:
        if refresh:
            refresh_devices()
        devices, eligible_indices, default_index = _resolve_input_devices()
    return [
        {"index": i, "name": devices[i]["name"], "is_default": i == default_index}
        for i in eligible_indices
    ]


def describe_default_input_device(refresh: bool = False) -> Optional[DeviceInfo]:
    """
    describe_default_input_device(refresh=False)
    Usage: call when you need the single device the app would record
    from right now, as {"index", "name", "is_default"} — for showing
    its name in the UI, or for logging which mic a session actually
    landed on after a hot-swap. Returns None only if the machine has no
    input devices at all.
    """
    devices = list_input_devices(refresh=refresh)
    if not devices:
        return None
    return next((d for d in devices if d["is_default"]), devices[0])


def _design_lowpass_kernel(cutoff_ratio: float, num_taps: int = 63) -> np.ndarray:
    """
    _design_lowpass_kernel(cutoff_ratio, num_taps)
    Usage: internal — called (and cached) once per distinct orig_sr by
    _resample_mono() below. Builds a windowed-sinc FIR low-pass filter
    with its cutoff at cutoff_ratio * Nyquist (cutoff_ratio is
    target_sr / orig_sr, i.e. the new Nyquist expressed as a fraction
    of the old one). Hamming-windowed sinc rather than a bare sinc so
    the stopband actually attenuates instead of ringing — a short,
    cheap FIR is plenty for speech-recognition input; this doesn't
    need to be audiophile-grade.
    """
    if num_taps % 2 == 0:
        num_taps += 1  # keep it symmetric around a center tap
    n = np.arange(num_taps) - (num_taps - 1) / 2
    sinc = np.sinc(cutoff_ratio * n)
    window = np.hamming(num_taps)
    kernel = (sinc * window).astype(np.float32)
    kernel /= kernel.sum()  # unity gain at DC
    return kernel


_LOWPASS_KERNEL_CACHE: dict = {}  # orig_sr -> precomputed kernel, so _resample_mono() isn't redesigning a filter ~10x/sec


def _resample_mono(samples: np.ndarray, orig_sr: float) -> np.ndarray:
    """
    _resample_mono(samples, orig_sr)
    Usage: internal — called from MicrophoneStream._callback() only
    when start() had to open the OS stream at a samplerate other than
    SAMPLE_RATE because the device rejected SAMPLE_RATE outright (see
    start()'s docstring). Azure Speech SDK and faster-whisper both
    require exactly SAMPLE_RATE regardless of what the physical device
    natively captures at, so every chunk is converted here before
    on_chunk ever sees it.

    When downsampling (orig_sr > SAMPLE_RATE — the common case, e.g. a
    headset that only offers 48kHz), first runs the signal through a
    low-pass filter cut off at the new Nyquist frequency before
    decimating. Skipping this step and going straight to linear
    interpolation (the previous implementation) is a well-documented
    source of aliasing: content above the new Nyquist folds back down
    into the audible band as artifacts rather than being discarded.
    Robust, well-resourced acoustic models (e.g. English) can shrug
    that off; this was confirmed in practice to specifically corrupt
    recognition of a less-resourced language (Persian) on the exact
    same headset/pipeline where English kept working fine — the
    symptom was garbled "guessed" transcriptions in one language only,
    which is exactly what aliasing artifacts riding along with the
    real signal would cause a speech model to mis-hear. Implemented as
    a plain windowed-sinc FIR via np.convolve rather than
    scipy/resampy, to add zero new packages to the PyInstaller build.
    """
    if samples.size == 0:
        return samples

    if orig_sr > SAMPLE_RATE:
        cutoff_ratio = SAMPLE_RATE / orig_sr
        kernel = _LOWPASS_KERNEL_CACHE.get(orig_sr)
        if kernel is None:
            kernel = _design_lowpass_kernel(cutoff_ratio)
            _LOWPASS_KERNEL_CACHE[orig_sr] = kernel
        samples = np.convolve(samples, kernel, mode="same").astype(np.float32)

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
        # The device index start() actually resolved and opened, which
        # differs from self.device_index whenever that was None ("follow
        # the OS default"). Read it to report or log which physical mic
        # is live right now.
        self.active_device_index: Optional[int] = None
        # time.monotonic() of the most recent audio callback. Polled by
        # the pipeline's watchdog: when a USB/wireless mic is yanked,
        # PortAudio frequently does not raise anything at all — the
        # callbacks just stop arriving and the session goes quiet with
        # no error to catch. A stalled clock here is the only reliable
        # signal that has happened. monotonic() so a system clock change
        # can't make a healthy stream look stalled.
        self._last_chunk_at: float = time.monotonic()
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

        # Stamped before anything else, and regardless of pause state:
        # this records that the DEVICE is still delivering audio, which
        # is a separate question from whether the app is currently doing
        # anything with it. Stamping it only when unpaused would make
        # every pause longer than the watchdog's threshold look like a
        # dead microphone.
        self._last_chunk_at = time.monotonic()

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
        global _open_stream_count

        # The whole open sequence runs under _portaudio_lock: it reads the
        # device list, then queries one device, then opens a stream on that
        # index — and a refresh_devices() landing anywhere in the middle
        # re-enumerates PortAudio and renumbers every device, so the index
        # resolved at the top would open some other piece of hardware, or
        # nothing at all. Holding the lock for the duration makes the
        # resolve-then-open pair atomic with respect to refreshes.
        with _portaudio_lock:
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
                self._last_chunk_at = time.monotonic()
                self._stream.start()
                self.active_device_index = device if isinstance(device, int) else None
                with _portaudio_lock:
                    _open_stream_count += 1
                return

            raise last_exc

    def stop(self) -> None:
        """
        stop()
        Usage: closes the OS audio stream. Safe to call even if start()
        was never called or the stream is already closed.

        Decrements the module-level open-stream count last and only if
        a stream was really open, since refresh_devices() reads it to
        decide whether re-enumerating devices would abort a live
        recording. Closing is done in a try/finally because a stream
        whose device was physically removed can throw on close, and
        leaking the count in that case would permanently block every
        later refresh — exactly when a refresh is most needed.
        """
        global _open_stream_count

        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001 - a removed device can throw on close
            print(f"[audio] error closing mic stream (device likely gone): {exc!r}", flush=True)
        finally:
            self._stream = None
            self.active_device_index = None
            with _portaudio_lock:
                _open_stream_count = max(0, _open_stream_count - 1)

    def seconds_since_audio(self) -> float:
        """
        seconds_since_audio()
        Usage: poll from a watchdog to detect a microphone that has
        stopped delivering audio without raising — the usual symptom of
        a USB or wireless mic being unplugged, switched off, or taken
        over by another app. A healthy stream refreshes this every
        BLOCK_SIZE samples (~100ms) whether or not anyone is speaking,
        so anything above a second or two means the device is gone
        rather than the room being quiet.
        """
        return time.monotonic() - self._last_chunk_at

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
