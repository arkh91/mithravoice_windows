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

Argos publishes its translation packages as a hub around English, so
most non-English pairs (French -> Spanish, Turkish -> Mandarin, ...)
have no direct package at all. This engine therefore resolves a ROUTE
rather than a single package: a direct hop when one exists, otherwise
two hops pivoting through English. Without that, every pair except the
ten en<->X ones failed outright — see _resolve_route().

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

from languages import PIVOT_ISO, to_iso639

from .base import ResultCallback, SpeechTranslator, TranslationResult

SAMPLE_RATE = 16000
_stanza_patched = False

# Process-wide cache of loaded faster-whisper models, keyed by the
# (path, device, compute_type) triple they were built from.
#
# This exists because OfflineEngine instances are disposable but the
# model they load is not cheap: importing faster_whisper pulls in
# ctranslate2 and its native extensions, and instantiating WhisperModel
# reads several hundred MB off disk. On a cold process that is tens of
# seconds, and it used to be paid again from scratch by every single
# engine instance — including the throwaway ones created by
# pipeline.py's fallback path, and including the ones whose start()
# the pipeline had already given up waiting on. That is what turned
# one slow first start into a run of repeated slow first starts: all
# the work of the abandoned attempt was discarded along with it.
#
# With the cache, an abandoned attempt still finishes in the
# background and still populates this dict, so the NEXT attempt gets
# the model for free and returns almost immediately. The observed
# "it failed four times and then suddenly worked" behaviour was
# exactly this happening by accident, via the OS file cache; making it
# explicit is what makes it reliable.
_model_cache: dict = {}
# Serializes ALL first-use initialization of this engine: the stanza
# patch, the argostranslate import, and the WhisperModel construction.
# One lock for all three rather than one per resource, because they are
# not independent — argostranslate imports stanza, stanza imports
# torch, and faster_whisper brings in ctranslate2, so two threads doing
# "different" parts of this are in fact fighting over the same native
# extensions and the same CPython import lock.
#
# Concurrently initializing native extensions from several threads is a
# documented way to crash the interpreter, not merely wasted memory,
# and a crash with no Python traceback is exactly what that looks like
# from the outside.
#
# RLock, because _warm_up() calls _load_whisper() which takes it again.
_init_lock = threading.RLock()

# True once _warm_up() has completed successfully. Like _stanza_patched,
# set at the END — a flag set on entry tells every later caller the work
# is done while it is still in progress.
_warmed = False


def _patch_stanza_for_offline_use() -> None:
    """
    _patch_stanza_for_offline_use()
    Usage: internal — called once, lazily, before the first Stanza
    pipeline is ever built (see _ensure_hop -> argostranslate.sbd's
    lazy_pipeline). Idempotent; safe to call repeatedly.

    Argos Translate uses Stanza for sentence-boundary detection on some
    language packages, and constructs it internally via
    stanza.Pipeline(...) with no option exposed to skip Stanza's own
    "is my resources.json index still current" check — so even with
    every model file bundled by prepare_offline_assets.py's
    download_stanza_resources(), stanza.Pipeline() still tries to fetch
    resources_<version>.json from raw.githubusercontent.com on EVERY
    construction, unconditionally. That network call has no timeout of
    its own, which is what was actually hanging offline sessions —
    bundling the models was necessary but not sufficient.

    Since argostranslate doesn't expose a way to pass
    download_method=stanza.DownloadMethod.REUSE_RESOURCES (which would
    ask Stanza the same thing from the outside), this patches Stanza's
    own file-fetch primitive instead: if the target file already
    exists on disk, use it as-is and skip the network call entirely,
    exactly like every other "trust the bundle first" check elsewhere
    in this file. Falls through to Stanza's normal (network) behavior
    for anything not already present — e.g. a language whose resources
    didn't get bundled (see prepare_offline_assets.py's per-language
    error handling) — so that case still fails/succeeds exactly as it
    would have without this patch, just without help.
    """
    global _stanza_patched
    if _stanza_patched:
        return

    # The flag is set at the END of this function, not the start.
    # Setting it first looks like ordinary reentrancy protection, but it
    # means any OTHER thread arriving while this one is still inside
    # `import stanza` returns immediately and carries on as though
    # stanza had been patched — when in fact request_file is still the
    # original, and that thread's first sentence split will go to the
    # network exactly as before. The import below is slow enough
    # (stanza pulls in torch) for that window to be tens of seconds
    # wide, so this is not a theoretical race. _init_lock is what makes
    # "wait for the first caller to finish" the actual behavior.
    try:
        import stanza.resources.common as _stanza_common
    except Exception as exc:  # noqa: BLE001 - stanza not installed/importable; nothing to patch
        _stanza_patched = True  # nothing to patch here ever; don't retry the failed import
        print(f"[offline] could not patch stanza for offline use (not installed?): {exc!r}", flush=True)
        return

    _original_request_file = _stanza_common.request_file

    def _request_file_bundled_first(url, path, *args, **kwargs):
        """
        Usage: internal — replaces stanza.resources.common.request_file.
        Same signature/behavior as the original, except it never
        touches the network for a file that's already sitting at
        `path` (which is exactly the case for everything
        prepare_offline_assets.py bundled). A missing file still falls
        through to the real network fetch, so this only removes
        unnecessary/unwanted network calls, never masks a genuine gap.
        """
        if Path(path).exists():
            return None
        return _original_request_file(url, path, *args, **kwargs)

    _stanza_common.request_file = _request_file_bundled_first
    _stanza_patched = True
    print("[offline] patched stanza to trust already-bundled resource files instead of "
          "re-checking them online on every use", flush=True)


FLUSH_INTERVAL = 0.6  # seconds between silence/finality checks (cheap — no transcription happens on most ticks)
MAX_BUFFER_SECONDS = 6.0  # force a final flush so a long run-on sentence still gets transcribed periodically
SILENCE_RMS_THRESHOLD = 250  # int16 RMS below this on the trailing 300ms counts as "quiet"


class OfflineEngineError(RuntimeError):
    """Raised when required offline models/packages can't be loaded."""


def _warm_up() -> None:
    """
    _warm_up()
    Usage: internal — does every piece of expensive first-use setup this
    engine needs, once per process, under _init_lock. Called at the top
    of OfflineEngine.start() and by preload(). Cheap and immediate on
    every call after the first.

    The three things it does are all slow, and the ORDER matters less
    than the fact that they happen together, on one thread, with
    everyone else waiting:

      1. patch stanza (which imports stanza, which imports torch);
      2. import argostranslate.translate and .package — the single
         biggest cost here, and the one that is easy to miss because
         nothing in this module imports it at module scope. It pulls in
         stanza and torch too;
      3. construct the WhisperModel (imports faster_whisper, hence
         ctranslate2, then reads the model off disk).

    Loading only the Whisper model — which is what the first version of
    preload() did — warms the cheapest of the three. In a real run the
    model was ready in 1.8 seconds while the engine start it was
    supposed to be helping still took over 45, because all the time was
    in step 2, waiting on a stanza/torch import that a background
    prewarm thread had already started and was still inside. Two
    threads, one CPython import lock, no progress visible to either.
    """
    global _warmed
    if _warmed:
        return
    with _init_lock:
        if _warmed:  # another thread finished while this one waited
            return
        started = time.monotonic()
        _patch_stanza_for_offline_use()
        # Imported here, eagerly and under the lock, rather than being
        # left to happen implicitly on whichever thread happens to call
        # _installed_pair_exists() first.
        import argostranslate.package  # noqa: F401 - imported for its side effect (loading the package)
        import argostranslate.translate  # noqa: F401 - same
        OfflineEngine()._load_whisper()
        _warmed = True
        print(f"[offline] engine warm-up complete in {time.monotonic() - started:.1f}s", flush=True)


def is_warm() -> bool:
    """
    is_warm()
    Usage: `if offline_engine.is_warm(): ...` — True once _warm_up() has
    finished, i.e. once starting this engine is a fast operation rather
    than a slow one. pipeline.py reads it to choose between the cold and
    warm start timeouts.
    """
    return _warmed


def preload() -> bool:
    """
    preload()
    Usage: call once, from a background daemon thread, when the app is
    doing something else anyway — pipeline.py calls it as soon as an
    ONLINE session starts (see TranslationPipeline._prewarm_offline).
    Returns True if the engine is warm afterwards.

    Nothing requires this; it only moves cost earlier. The point is
    WHEN the offline engine's cold start is otherwise paid: at the exact
    moment the network dropped mid-sentence and the user is waiting to
    hear the rest of it. That is the worst possible time to spend half a
    minute importing torch. Doing it while Azure is healthy and the CPU
    is otherwise idle means the fallback, when it comes, is nearly free.

    Crucially this shares _init_lock with the real start(), so a
    fallback that arrives mid-prewarm WAITS for it rather than racing
    it. Racing was strictly worse than not prewarming at all: both
    threads contended on the same imports, neither finished sooner, and
    two threads initializing the same native extensions is the one thing
    all of this is built to avoid.

    Swallows every exception — a failed prewarm must be invisible, since
    the real start() will report any genuine problem properly and this
    runs where nobody is waiting on an answer.
    """
    try:
        _warm_up()
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort warm-up; start() does the real reporting
        print(f"[offline] prewarm skipped: {exc!r}", flush=True)
        return False


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
        # The ordered list of ISO codes translation hops through, e.g.
        # ["en", "fa"] for a direct pair or ["fr", "en", "es"] for one
        # pivoted through English. Empty when from == to (no-op).
        self._route: List[str] = []
        self._on_result: Optional[ResultCallback] = None

    def _load_whisper(self):
        """
        _load_whisper()
        Usage: internal — returns a faster_whisper.WhisperModel, loading
        it on first use and reusing it for every later engine instance
        in this process (see _model_cache). Prefers a bundled,
        pre-downloaded ctranslate2 model directory
        (settings.whisper_model_path, populated by
        scripts/prepare_offline_assets.py before packaging) so the
        installed app needs zero network access. Falls back to
        downloading by model size — convenient for `python main.py`
        during development, but NOT what ships in the Windows build.

        The cache is what makes a second start fast. It also means the
        model survives a start attempt the pipeline stopped waiting
        for: that attempt's thread keeps running, finishes the load,
        and leaves the result here for whoever asks next.
        """
        from faster_whisper import WhisperModel

        from config import settings

        bundled_path = Path(settings.whisper_model_path)
        source = str(bundled_path) if bundled_path.exists() else settings.whisper_model_size
        key = (source, settings.whisper_device, settings.whisper_compute_type)

        # Cheap read first, so the common (already-loaded) case never
        # waits behind another thread's in-progress load.
        cached = _model_cache.get(key)
        if cached is not None:
            return cached

        with _init_lock:
            cached = _model_cache.get(key)  # may have been filled while waiting for the lock
            if cached is not None:
                return cached
            started = time.monotonic()
            print(f"[offline] loading whisper model from {source!r} (first use this session)", flush=True)
            model = WhisperModel(
                source,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
            print(f"[offline] whisper model ready in {time.monotonic() - started:.1f}s", flush=True)
            _model_cache[key] = model
            return model

    @staticmethod
    def _installed_pair_exists(from_iso: str, to_iso: str) -> bool:
        """
        _installed_pair_exists(from_iso, to_iso)
        Usage: internal — True when Argos already has a DIRECT translation
        installed for this hop. Replaces the previous nested-generator
        check, which called next() over the installed-language list once
        per candidate language and raised StopIteration on some orderings.
        """
        import argostranslate.translate

        by_code = {lang.code: lang for lang in argostranslate.translate.get_installed_languages()}
        source, target = by_code.get(from_iso), by_code.get(to_iso)
        if source is None or target is None:
            return False
        return source.get_translation(target) is not None

    def _install_bundled_hop(self, from_iso: str, to_iso: str) -> bool:
        """
        _install_bundled_hop(from_iso, to_iso)
        Usage: internal — installs models/argos/{from}_{to}.argosmodel if
        that file was bundled by scripts/prepare_offline_assets.py.
        Returns True on success, False when no such file exists. Matches
        on the exact stem rather than a substring so a code that is a
        prefix of another can never install the wrong package.
        """
        import argostranslate.package

        from config import settings

        bundled_dir = Path(settings.argos_packages_dir)
        candidate = bundled_dir / f"{from_iso}_{to_iso}.argosmodel"
        if not candidate.exists():
            return False
        argostranslate.package.install_from_path(str(candidate))
        return True

    @staticmethod
    def _install_downloaded_hop(from_iso: str, to_iso: str) -> bool:
        """
        _install_downloaded_hop(from_iso, to_iso)
        Usage: internal — DEV-ONLY fallback that fetches a hop from the
        Argos package index over the network. Returns False rather than
        raising when the pair doesn't exist or the network is unreachable,
        so _resolve_route() can go on to try the pivot route instead of
        the whole session dying on one missing direct package. A shipped
        build should never reach this: prepare_offline_assets.py bundles
        every hop the UI can ask for.
        """
        import argostranslate.package

        # This is the one call in the whole offline engine that hits
        # the network with no timeout of its own — worth a log line so
        # a hang here is traceable, the same way _wait_for_connection
        # and is_online() elsewhere explain themselves before a slow
        # network operation. A shipped build should never reach this
        # at all (see this method's docstring); if it does, that's the
        # thing to investigate first.
        #
        # Ask connectivity first. The single most likely reason this
        # engine is being started at all is that the network just went
        # away, which is precisely the condition under which an
        # untimed network call doesn't fail — it hangs. Skipping it
        # outright when there is demonstrably nothing to reach turns a
        # multi-second (or unbounded) stall into an immediate, honest
        # "no route for this pair", which is the answer either way.
        import connectivity

        if not connectivity.is_online():
            print(f"[offline] no bundled/installed package for {from_iso}->{to_iso}, and no network "
                  f"to check the Argos package index — treating the hop as unavailable", flush=True)
            return False

        print(f"[offline] no bundled/installed package for {from_iso}->{to_iso}; "
              f"checking the Argos package index online", flush=True)
        try:
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
        except Exception:  # noqa: BLE001 - offline machine; not an error, just no network route
            return False

        match = next((p for p in available if p.from_code == from_iso and p.to_code == to_iso), None)
        if match is None:
            return False
        argostranslate.package.install_from_path(match.download())
        return True

    def _ensure_hop(self, from_iso: str, to_iso: str) -> bool:
        """
        _ensure_hop(from_iso, to_iso)
        Usage: internal — makes one direct from->to translation available,
        trying in order: already installed, bundled .argosmodel, network
        download. Returns whether the hop is usable afterwards.
        """
        if self._installed_pair_exists(from_iso, to_iso):
            return True
        if self._install_bundled_hop(from_iso, to_iso):
            return True
        if self._install_downloaded_hop(from_iso, to_iso):
            return True
        return False

    def _resolve_route(self, from_iso: str, to_iso: str) -> List[str]:
        """
        _resolve_route(from_iso, to_iso)
        Usage: internal — called once in start(). Returns the list of ISO
        codes translation will hop through, installing whatever packages
        that route needs.

        Argos ships packages as a hub around English, so only en<->X pairs
        generally exist. A direct hop is preferred when there is one
        (fewer hops, no compounding of translation error); otherwise the
        route pivots through English, which is what makes French->Spanish,
        Turkish->Mandarin and the other 40-odd cross pairs work at all
        instead of raising "no package available".

        Raises OfflineEngineError only when BOTH routes are unavailable,
        with a message naming the specific hop that's missing so the
        fix (re-run prepare_offline_assets.py) is obvious from the log.
        """
        if from_iso == to_iso:
            return []  # nothing to translate; _translate_text passes the text straight through

        if self._ensure_hop(from_iso, to_iso):
            return [from_iso, to_iso]

        if PIVOT_ISO not in (from_iso, to_iso):
            first_leg = self._ensure_hop(from_iso, PIVOT_ISO)
            second_leg = self._ensure_hop(PIVOT_ISO, to_iso)
            if first_leg and second_leg:
                return [from_iso, PIVOT_ISO, to_iso]
            missing = []
            if not first_leg:
                missing.append(f"{from_iso}->{PIVOT_ISO}")
            if not second_leg:
                missing.append(f"{PIVOT_ISO}->{to_iso}")
            raise OfflineEngineError(
                f"No offline route for {from_iso} -> {to_iso}: missing Argos package(s) "
                f"{', '.join(missing)}. Run scripts/prepare_offline_assets.py to bundle them."
            )

        raise OfflineEngineError(
            f"No offline route for {from_iso} -> {to_iso}: missing Argos package {from_iso}->{to_iso}. "
            f"Run scripts/prepare_offline_assets.py to bundle it."
        )

    def start(self, from_lang: str, to_lang: str, on_result: ResultCallback) -> None:
        """
        start(from_lang, to_lang, on_result)
        Usage: see SpeechTranslator.start. Loads the whisper model and
        resolves the Argos translation route for the pair — direct if a
        package exists, otherwise pivoting through English — then starts
        the background flush thread that drives transcription. Raises
        OfflineEngineError if no route is available; pipeline.py catches
        that and reports it to the UI.
        """
        # One call covering the stanza patch, the argostranslate import
        # and the Whisper model load — see _warm_up(). Blocks if a
        # prewarm (or another start) is already doing that work, rather
        # than duplicating it on this thread.
        _warm_up()

        self._from_iso = to_iso639(from_lang)
        self._to_iso = to_iso639(to_lang)
        self._on_result = on_result

        if self._model is None:
            self._model = self._load_whisper()  # warm by now; a cache lookup
        self._route = self._resolve_route(self._from_iso, self._to_iso)

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

            try:
                self._transcribe_and_translate(chunks, is_final=True)
            except Exception as exc:  # noqa: BLE001 - one bad utterance must not end the session
                # This thread IS the offline engine while a session
                # runs. An uncaught exception here doesn't crash
                # anything visible — it just silently ends the thread,
                # after which audio keeps being buffered by feed() and
                # no caption ever appears again, with nothing in the
                # log to say why. Report and keep going: the next
                # utterance usually transcribes fine.
                print(f"[offline] failed to transcribe/translate one segment: {exc!r}", flush=True)

    def _transcribe_and_translate(self, chunks: List[bytes], is_final: bool) -> None:
        """
        _transcribe_and_translate(chunks, is_final)
        Usage: internal — runs whisper on the given PCM chunks, translates
        the resulting text with Argos, and calls on_result. Silently
        skips empty transcriptions (e.g. pure silence) rather than
        emitting a blank subtitle.
        """
        audio = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language=self._from_iso, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text:
            return

        translated = self._translate_text(text)
        if self._on_result:
            self._on_result(TranslationResult(text, translated, is_final=is_final))

    def _translate_text(self, text: str) -> str:
        """
        _translate_text(text)
        Usage: internal — runs text through every hop of the route
        resolved in start(). One hop for a direct pair, two when pivoting
        through English. Returns the text unchanged when from == to
        (empty route), which is the only sane thing to do if the UI ever
        allows the same language on both sides.
        """
        import argostranslate.translate

        result = text
        for source, target in zip(self._route, self._route[1:]):
            result = argostranslate.translate.translate(result, source, target)
        return result

    def stop(self) -> None:
        """
        stop()
        Usage: see SpeechTranslator.stop. Stops the flush thread and
        drops any unflushed buffered audio (a paused/stopped session
        shouldn't emit a stale subtitle later).
        """
        self._running.clear()
        self._route = []
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=FLUSH_INTERVAL + 1)
            self._flush_thread = None
        with self._buffer_lock:
            self._buffer.clear()
            self._buffer_started_at = None
