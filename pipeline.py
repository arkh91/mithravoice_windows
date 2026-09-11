"""
pipeline.py

Wires MicrophoneStream to a translation engine and pushes results back
out through a single callback. Owns the online/offline engine choice:
probes real connectivity before deciding, prefers AzureEngine when the
network is actually there, drops to OfflineEngine when it isn't or
when Azure fails mid-session, and climbs back to Azure once the network
returns — the UI only ever sees TranslationResults and, optionally, an
engine-mode change notification.

The engine choice follows one rule, in this order:

  1. Not connected at start -> offline immediately. No Azure attempt,
     no waiting out a timeout on a network that was never going to
     answer. This is the case the old code handled worst: it always
     tried Azure first and let it fail, which cost seconds of dead air
     at the start of every offline session.
  2. Connected at start -> online (when the plan allows it).
  3. Connection lost mid-session -> offline, without dropping the mic.
     This is detected two ways: reactively, if AzureEngine's SDK
     itself reports a cancellation (a clean drop); and proactively,
     via the watchdog's own periodic reachability probe (see
     ONLINE_HEALTH_PROBE_SECONDS), for the silent drops — Wi-Fi
     switched off, a cable pulled, a captive portal — that the SDK may
     take a long time to notice, or never explicitly report at all.
     Without the second path, a silent drop left the session "online"
     with audio going nowhere: translation froze with no visible error.
  4. Connection comes back -> online again, without dropping the mic.

Usage:
    pipeline = TranslationPipeline(on_result=push_to_ui, on_engine_change=notify_ui, on_level=push_level_to_ui)
    pipeline.start(from_lang="English", to_lang="Persian (Farsi)", device_index=None, mode="auto")
    pipeline.set_paused(True)
    pipeline.stop()
"""

import threading
import time
from typing import Callable, Literal, Optional

import audio
import connectivity
from audio import MicrophoneStream
from engines.azure_engine import AzureEngine
from engines.base import ResultCallback, SpeechTranslator, TranslationResult
from engines.offline_engine import OfflineEngine, OfflineEngineError

EngineMode = Literal["auto", "online", "offline"]
EngineChangeCallback = Callable[[str], None]  # receives "online" | "offline" | "failed"
LevelCallback = Callable[[float], None]  # receives 0.0-1.0 per audio chunk
DeviceChangeCallback = Callable[[Optional[dict]], None]  # receives {"index", "name"} or None

# How often the watchdog checks engine errors, the default recording
# device, and whether audio is still arriving. One second is well under
# what a person perceives as "it didn't recover", and each tick is three
# cheap checks.
WATCHDOG_INTERVAL_SECONDS = 1.0

# How long the mic stream may go without delivering a single callback
# before it's treated as dead and reopened. A healthy stream calls back
# roughly every 100ms even in silence, so this is ~25 missed callbacks —
# high enough that a scheduling hiccup or a moment of heavy CPU load
# can't trigger a needless reconnect.
AUDIO_STALL_SECONDS = 2.5

# How long to wait between probes when the session has dropped to
# offline because of a network failure and is waiting to climb back.
# Each probe is a TLS handshake, so this is not free; fifteen seconds
# is fast enough that a brief Wi-Fi blip is recovered from within one
# sentence, and slow enough that a genuinely offline machine isn't
# handshaking at a dead host all day.
RECONNECT_PROBE_SECONDS = 15.0

# How often the watchdog independently re-verifies reachability WHILE
# the session is nominally online, instead of waiting to be told.
#
# Without this, the only thing that can ever trigger a fallback is
# AzureEngine's own `canceled` event (see azure_engine.py's
# _on_canceled) — and that event only fires once the SDK's underlying
# transport has itself noticed the connection is gone. A clean
# disconnect (server closes the socket, a proxy resets it) is noticed
# almost immediately. A SILENT one — Wi-Fi turned off, an ethernet
# cable pulled, a captive portal that just stops routing — is not: the
# local socket has no way to know the far end is unreachable until
# something tries to use it and times out, and push_stream.write()
# (see AzureEngine.feed) only writes into the SDK's local buffer, so
# it keeps "succeeding" the entire time. The mic keeps recording, the
# UI keeps showing "online", and no audio is actually reaching
# anyone — which is exactly the freeze this constant exists to catch.
# Independently confirming reachability every few seconds, rather than
# only reacting to what the engine chooses to report, closes that gap.
ONLINE_HEALTH_PROBE_SECONDS = 8.0

# How long the pipeline waits for OfflineEngine.start() before giving
# up on it, rather than trusting it to always return promptly.
#
# It shouldn't need this: a properly bundled offline engine loads a
# model from local disk and never touches the network. But config.py's
# whisper_model_path falls back to auto-downloading from Hugging Face
# "for dev convenience" whenever the bundled model directory isn't
# present — and that download uses the standard requests/urllib3
# stack, which has no timeout of its own. Precisely the one moment
# this fallback path is guaranteed to run without internet — falling
# back to offline BECAUSE the network is gone — is the one moment a
# network call with no timeout can hang forever. Without this bound,
# that hang froze the whole pipeline: the "connecting" badge (see
# _restart_on_offline) never resolved, no error ever printed, and the
# session sat there translating nothing with no indication why.
#
# This can't forcibly kill that stuck call — Python has no safe way to
# do that — it only bounds how long the PIPELINE waits for an answer,
# so the UI gets a definite result (offline is ready, or it failed)
# instead of hanging indefinitely. See _start_engine_bounded().
#
# There are two of these, because a cold start and a warm one are not
# the same operation and one number cannot describe both.
#
# COLD: the first offline start in this process has to import
# faster_whisper (and through it ctranslate2's native extensions) and
# read a multi-hundred-megabyte model off disk. On a modest laptop
# that is comfortably over twenty seconds with no network involved at
# all. The old single 6s bound was shorter than the legitimate work,
# so it "timed out" an engine that was fine — and then, because the
# attempt it abandoned kept running and kept the slot busy, it refused
# every retry for the next 45 seconds too. That is how a slow start
# turned into a minute-long outage with four scary tracebacks: at no
# point was anything actually broken.
OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS = 45.0

# WARM: once the model is in engines.offline_engine's process-wide
# cache, start() is a cache lookup plus an Argos route resolve. If
# THAT takes more than a few seconds something really is wrong —
# almost certainly a network call that shouldn't be happening — so the
# tight bound the original constant was reaching for is right here,
# where it was always meant to apply.
OFFLINE_ENGINE_WARM_START_TIMEOUT_SECONDS = 8.0

# Ceiling on how long an in-flight offline-start attempt (see the
# bookkeeping in __init__/_start_engine_bounded) is trusted as "still
# legitimately in progress" before a new attempt is allowed to proceed
# alongside it. Must stay comfortably above the cold-start timeout, or
# it would declare a perfectly healthy first load abandoned partway
# through and start a second one on top of it — the exact concurrent
# native-extension initialization this bookkeeping exists to prevent.
OFFLINE_START_LOCK_STALE_SECONDS = 120.0

# How long to wait before the watchdog retries an offline fallback that
# failed to start. Short, because the usual reason for the failure is a
# first-use load that simply hadn't finished yet and is still running —
# so the retry mostly just has to show up and wait for it.
OFFLINE_RETRY_AFTER_SECONDS = 5.0


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
        on_device_change: Optional[DeviceChangeCallback] = None,
    ):
        self._on_result = on_result
        self._on_engine_change = on_engine_change
        self._on_level = on_level
        self._on_device_change = on_device_change
        self._mic: Optional[MicrophoneStream] = None
        self._engine: Optional[SpeechTranslator] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_running = threading.Event()
        self._from_lang = "English"
        self._to_lang = "Persian (Farsi)"
        self._device_index: Optional[int] = None
        self._mode: EngineMode = "auto"
        self._lock = threading.Lock()
        # True when start() was given device_index=None, i.e. "whatever
        # Windows currently calls the default recording device" rather
        # than one specific pinned mic. Only in that case may the
        # watchdog move the session onto a different device by itself —
        # a user who explicitly pinned a mic should not have it silently
        # changed out from under them.
        self._follow_default = True
        # The Core Audio endpoint ID of the default recording device as
        # of the last check. A change here is what triggers a hot-swap
        # (see _watchdog_loop). None on non-Windows, where the stall
        # detector is the only signal available.
        self._last_endpoint_id: Optional[str] = None
        # Mirrors the Pause button so a mic swapped in mid-session comes
        # back paused if that's how the session was left, instead of
        # quietly resuming translation the user had stopped.
        self._paused = False
        # True when this session is running offline ONLY because the
        # network wasn't available, rather than because the user or
        # their plan asked for offline. Only such a session is eligible
        # to climb back to Azure — a deliberate offline session must
        # never be moved online behind the user's back, and on a
        # metered plan that would also start billing they didn't ask
        # for.
        self._offline_due_to_network = False
        # monotonic() of the last reconnect probe, so the watchdog can
        # tick every second for its other checks without probing the
        # network every second too.
        self._last_reconnect_probe: float = 0.0
        # monotonic() of the last independent reachability check taken
        # WHILE online (see ONLINE_HEALTH_PROBE_SECONDS above). Separate
        # from _last_reconnect_probe, which only matters once we've
        # already fallen back — this one is what notices the fall in
        # the first place when Azure's own SDK is slow to say so.
        self._last_online_probe: float = 0.0
        # Bookkeeping for _start_engine_bounded(). Only ONE offline
        # engine start may be in flight at a time: each one
        # independently imports and initializes heavy native extensions
        # (ctranslate2, stanza's dependencies), and racing that from
        # several threads at once produced an interpreter-level crash in
        # practice, not merely wasted CPU.
        #
        # The important change from the original design is what a
        # SECOND caller does about that. It used to raise — "an earlier
        # attempt is still running, try again later" — which was the
        # wrong answer twice over: the earlier attempt was usually
        # about to succeed, and the caller usually wanted exactly the
        # engine that attempt was building. So the refusal manufactured
        # a failure out of a success that hadn't landed yet, and the
        # user saw a traceback for a working engine. Now a second
        # caller ATTACHES to the in-flight attempt instead: same
        # thread, same engine, same outcome dict, just another waiter.
        # Nothing is started twice, nothing is thrown away, and the
        # slow first load is paid once by whoever asks first rather
        # than once per click.
        #
        # Tracked with plain state plus a small bookkeeping lock rather
        # than a bare threading.Lock, because a Lock has no owner and no
        # way to ask "how long has this been held", both of which
        # OFFLINE_START_LOCK_STALE_SECONDS needs.
        self._offline_start_state_lock = threading.Lock()
        self._offline_start_generation = 0
        self._offline_start_pending = False
        self._offline_start_started_at: Optional[float] = None
        # The thread running the in-flight attempt, the engine it is
        # starting, and the dict it will record its outcome in — the
        # three things a late-arriving second caller needs in order to
        # wait on that attempt and adopt its result instead of starting
        # a competing one.
        self._offline_start_thread: Optional[threading.Thread] = None
        self._offline_start_engine: Optional[SpeechTranslator] = None
        self._offline_start_outcome: Optional[dict] = None
        # An engine that finished starting AFTER everyone had stopped
        # waiting for it, stored with the language pair it was started
        # for, so the next attempt can claim it instead of building
        # another. Two problems solved at once:
        #
        #   * the next attempt becomes instant, which is what makes the
        #     watchdog retry after a timed-out fallback reliable rather
        #     than hopeful;
        #   * the engine doesn't leak. A started OfflineEngine owns a
        #     running flush thread; abandoning one left that thread
        #     transcribing into a callback nobody was reading, forever.
        self._offline_unclaimed_engine: Optional[SpeechTranslator] = None
        self._offline_unclaimed_pair: Optional[tuple] = None
        # Live count of callers blocked on the in-flight attempt; see
        # _start_engine_bounded. Replaced per attempt.
        self._offline_start_waiters: dict = {"n": 0}
        # True once any offline start has completed successfully in this
        # process, which is what distinguishes a cold start (model not
        # yet loaded, legitimately slow) from a warm one (cache hit,
        # should be near-instant) when choosing a timeout. See
        # OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS.
        self._offline_started_before = False
        # Set once per process so the background prewarm (see
        # _prewarm_offline) runs at most once.
        self._offline_prewarm_started = False
        # monotonic() after which the watchdog should retry an offline
        # fallback that failed, or None when no retry is owed.
        #
        # Without this a timed-out fallback was terminal: _engine was
        # left None, so neither _should_try_online_health_check (wants
        # an AzureEngine) nor _should_try_reconnect (wants an
        # OfflineEngine) could ever fire again, and the watchdog span on
        # doing nothing while the mic stayed open and the UI said
        # "failed". The session could only be recovered by hand.
        #
        # A retry is nearly free and very likely to succeed, because the
        # attempt that timed out is still running in the background:
        # the retry attaches to it (see _start_engine_bounded) and
        # collects the engine it was always going to produce.
        self._offline_retry_at: Optional[float] = None

    def _make_engine(self, prefer_online: bool) -> SpeechTranslator:
        """
        _make_engine(prefer_online)
        Usage: internal — returns a fresh, unstarted engine instance
        based on prefer_online and the pipeline's configured mode.
        """
        if self._mode == "offline" or not prefer_online:
            return OfflineEngine()
        return AzureEngine()

    def _offline_start_timeout(self) -> float:
        """
        _offline_start_timeout()
        Usage: internal — how long to wait for OfflineEngine.start()
        this time round. The cold-start budget on the first successful
        start of the process, the much tighter warm budget afterwards.
        See the two constants for why one number can't serve both.
        """
        # Ask the engine module, not just this pipeline's own history: a
        # successful background prewarm makes the next start fast even
        # though no start has completed here yet, and the tighter bound
        # should apply then too.
        import engines.offline_engine as offline_engine

        if self._offline_started_before or offline_engine.is_warm():
            return OFFLINE_ENGINE_WARM_START_TIMEOUT_SECONDS
        return OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS

    def _prewarm_offline(self) -> None:
        """
        _prewarm_offline()
        Usage: internal — called when an ONLINE session starts, to load
        the offline engine's Whisper model on a background daemon
        thread while nothing needs it yet. Runs at most once per
        process and never raises.

        This is the fix for the worst version of the cold start: the
        network drops mid-sentence, the pipeline falls back, and the
        user waits out a full model load with the caption frozen.
        Paying that cost up front, while Azure is healthy and the
        machine is otherwise idle, turns the fallback into a cache hit.
        Skipped entirely when the plan or the user asked for online
        only, since no fallback can happen there and the memory would
        be spent for nothing.
        """
        if self._offline_prewarm_started or self._mode != "auto":
            return
        self._offline_prewarm_started = True

        def _run() -> None:
            import engines.offline_engine as offline_engine

            if offline_engine.preload():
                print("[pipeline] offline engine prewarmed; a fallback will now be immediate", flush=True)

        threading.Thread(target=_run, daemon=True, name="offline-engine-prewarm").start()

    def _start_engine_bounded(self, engine: SpeechTranslator, timeout: Optional[float] = None) -> SpeechTranslator:
        """
        _start_engine_bounded(engine, timeout=None)
        Usage: internal — like calling `engine.start(from_lang, to_lang,
        on_result)` directly, except it gives up waiting after `timeout`
        seconds instead of trusting the engine to always return
        promptly. Used for OfflineEngine specifically — see
        OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS for why that one
        engine, of the two, needs this. Pass timeout=None to use
        _offline_start_timeout()'s cold/warm choice, which is what
        every caller should do.

        RETURNS the engine that actually got started. That is usually
        the one passed in, but not always: if another attempt was
        already in flight, this waits for THAT attempt and returns ITS
        engine, and the one passed in is discarded unstarted. Callers
        must use the return value rather than assuming their own
        instance is the live one.

        Runs start() on a background daemon thread and joins it with a
        deadline. On timeout, raises OfflineEngineError and simply stops
        waiting — it does NOT and cannot kill the background thread.
        That thread keeps running, and is deliberately left to finish:
        its real product is the loaded Whisper model, which it writes
        into engines.offline_engine's process-wide cache, so even an
        attempt nobody is waiting on any more makes the next one fast.

        When an attempt is already in flight, this ATTACHES to it —
        same thread, same outcome — instead of either starting a
        competing one or refusing. Refusing is what the original
        version did, and it was the direct cause of the symptom this
        was written to fix: one slow first load left the slot busy, and
        every start for the next 45 seconds raised "an earlier attempt
        is still running" at a user whose engine was, at that moment,
        finishing loading perfectly well.

        The one case where a second attempt does start anyway is an
        in-flight one older than OFFLINE_START_LOCK_STALE_SECONDS —
        genuinely stuck, not merely slow. Without that escape hatch a
        single truly-dead attempt would block the offline engine for
        the rest of the app's life. A generation counter, not the raw
        pending flag, is what gets cleared on completion, so a
        very-late-finishing abandoned attempt can never mark a newer,
        genuinely in-progress attempt as done.
        """
        if timeout is None:
            timeout = self._offline_start_timeout()

        attach_to: Optional[tuple] = None

        with self._offline_start_state_lock:
            # Claim a previously-abandoned-but-successful engine before
            # doing anything else. It is already started, already wired
            # to this pipeline's result callback, and already running
            # its flush thread — so the only alternatives are using it
            # or shutting it down, and using it is free.
            unclaimed = self._offline_unclaimed_engine
            if unclaimed is not None:
                if self._offline_unclaimed_pair == (self._from_lang, self._to_lang):
                    self._offline_unclaimed_engine = None
                    self._offline_unclaimed_pair = None
                    self._offline_started_before = True
                    print("[pipeline] claiming the offline engine that finished starting earlier", flush=True)
                    return unclaimed
                # Started for a different language pair, so it can't be
                # used — but it still owns a live flush thread, so it
                # has to be stopped rather than merely dropped.
                self._offline_unclaimed_engine = None
                self._offline_unclaimed_pair = None
                try:
                    unclaimed.stop()
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    print(f"[pipeline] could not stop a stale offline engine: {exc!r}", flush=True)

            if self._offline_start_pending and self._offline_start_thread is not None:
                running_for = time.monotonic() - (self._offline_start_started_at or time.monotonic())
                if running_for < OFFLINE_START_LOCK_STALE_SECONDS:
                    print(
                        f"[pipeline] an offline engine start is already in flight "
                        f"({running_for:.0f}s so far); waiting for it instead of starting a second one",
                        flush=True,
                    )
                    self._offline_start_waiters["n"] += 1
                    attach_to = (
                        self._offline_start_thread,
                        self._offline_start_engine,
                        # `is not None`, NOT `or {}`: the outcome dict is
                        # empty until the attempt finishes, and an empty
                        # dict is falsy — so `or {}` handed the attacher
                        # a different, permanently-empty dict and it
                        # never saw the result it was waiting for.
                        self._offline_start_outcome if self._offline_start_outcome is not None else {},
                        running_for,
                    )
                else:
                    print(
                        f"[pipeline] a previous offline engine start attempt has been running for "
                        f"{running_for:.0f}s with no sign of finishing — treating it as abandoned and "
                        f"starting a new attempt anyway",
                        flush=True,
                    )

            if attach_to is None:
                self._offline_start_generation += 1
                my_generation = self._offline_start_generation
                outcome: dict = {}
                # How many callers are currently blocked waiting on this
                # attempt. Read by _run() to decide whether its result
                # has an owner or needs parking (see
                # _offline_unclaimed_engine). Mutated only under
                # _offline_start_state_lock.
                waiters = {"n": 1}
                self._offline_start_waiters = waiters
                self._offline_start_pending = True
                self._offline_start_started_at = time.monotonic()
                self._offline_start_engine = engine
                self._offline_start_outcome = outcome

        if attach_to is not None:
            thread, inflight_engine, inflight_outcome, running_for = attach_to
            # Wait out whatever the in-flight attempt has left of its
            # own budget, not a fresh full one — otherwise two clicks
            # ten seconds apart would extend the total wait rather than
            # sharing it.
            remaining = max(1.0, timeout - running_for)
            thread.join(remaining)
            with self._offline_start_state_lock:
                self._offline_start_waiters["n"] -= 1
            if thread.is_alive():
                raise OfflineEngineError(
                    f"the offline engine has been starting for {running_for + remaining:.0f}s and still "
                    f"hasn't finished. It is left running in the background, so trying again shortly "
                    f"should be much faster."
                )
            if "error" in inflight_outcome:
                raise inflight_outcome["error"]
            if inflight_engine is None:
                raise OfflineEngineError("the in-flight offline engine start finished without producing an engine")
            self._offline_started_before = True
            return inflight_engine

        def _run() -> None:
            """
            _run()
            Usage: internal — the body of the background start thread.
            Records its outcome where both the launching caller and any
            later attacher can read it, and clears the in-flight slot
            on the way out.
            """
            try:
                engine.start(self._from_lang, self._to_lang, self._on_engine_result)
            except Exception as exc:  # noqa: BLE001 - re-raised on whichever thread is waiting
                outcome["error"] = exc
            else:
                outcome["done"] = True
                with self._offline_start_state_lock:
                    # Nobody is waiting on this any more — the caller
                    # gave up and the result would otherwise be thrown
                    # away with its flush thread still running. Park it
                    # for the next attempt to claim.
                    if self._offline_start_generation == my_generation and not waiters["n"]:
                        self._offline_unclaimed_engine = engine
                        self._offline_unclaimed_pair = (self._from_lang, self._to_lang)
                        print(
                            "[pipeline] the offline engine finished starting after its caller had "
                            "given up; keeping it ready for the next attempt",
                            flush=True,
                        )
            finally:
                with self._offline_start_state_lock:
                    # Only clear the slot if nothing newer has since
                    # started (see the "treat as abandoned" branch
                    # above). Otherwise a very-late-finishing abandoned
                    # attempt could stomp on a current, genuinely
                    # in-progress one's state.
                    if self._offline_start_generation == my_generation:
                        self._offline_start_pending = False
                        self._offline_start_thread = None
                        self._offline_start_engine = None
                        self._offline_start_outcome = None

        thread = threading.Thread(target=_run, daemon=True, name="offline-engine-start")
        with self._offline_start_state_lock:
            self._offline_start_thread = thread
        thread.start()
        thread.join(timeout)
        with self._offline_start_state_lock:
            waiters["n"] -= 1

        if thread.is_alive():
            # Raised as OfflineEngineError, not a bare TimeoutError, so
            # api.py's start_session() routes this into its dedicated
            # OfflineEngineError handler (-> "offline_engine_unavailable")
            # instead of the generic catch-all, which reports
            # "mic_start_failed" — a confusing, wrong-half-of-the-story
            # message for a problem that has nothing to do with the mic
            # (see that handler's own comment for the exact same lesson
            # learned about AZURE_SPEECH_KEY vs. the offline engine).
            #
            # The thread is left running on purpose: it is still loading
            # the model into the shared cache, which is what makes the
            # retry this message suggests actually work.
            raise OfflineEngineError(
                f"the offline engine did not finish starting within {timeout:.0f}s. "
                f"It is still loading in the background — trying again shortly should succeed. "
                f"If it never does, run: python scripts/diagnose.py"
            )
        if "error" in outcome:
            raise outcome["error"]

        self._offline_started_before = True
        return engine

    def _start_engine(self, prefer_online: bool) -> None:
        """
        _start_engine(prefer_online)
        Usage: internal — instantiates and starts an engine, falling back
        to OfflineEngine once if the online attempt fails. Notifies
        on_engine_change so the UI can reflect which engine ended up
        active.

        Checks real connectivity BEFORE attempting Azure. Without this,
        starting a session on a disconnected machine meant building a
        recognizer, opening a connection, and waiting out the full
        connect timeout before failing over — seconds of silence at the
        top of every offline session, for an answer that was knowable
        immediately. connectivity.is_online() probes the actual speech
        endpoint rather than asking the OS whether an adapter is up,
        which is the distinction that matters on a VPN, behind a proxy,
        or on a captive-portal Wi-Fi.
        """
        if prefer_online and self._mode != "offline":
            if not connectivity.is_online(force=True):
                print("[pipeline] no connectivity to Azure; starting on the offline engine", flush=True)
                prefer_online = False
                self._offline_due_to_network = True

        engine = self._make_engine(prefer_online)
        active_mode = "offline" if isinstance(engine, OfflineEngine) else "online"
        if active_mode == "offline" and self._on_engine_change:
            # Loading Whisper/Argos (see OfflineEngine.start) is real
            # work — first-use model loads in particular can take a
            # few seconds — so tell the UI a switch is underway right
            # away rather than leaving "Listening…" up, unexplained,
            # for however long that turns out to take.
            self._on_engine_change("connecting")
        try:
            if active_mode == "offline":
                # Reassigned from the return value, not discarded: if
                # another start was already in flight, THAT attempt's
                # engine is the live one and this instance was never
                # started (see _start_engine_bounded).
                engine = self._start_engine_bounded(engine)
            else:
                engine.start(self._from_lang, self._to_lang, self._on_engine_result)
        except Exception as exc:  # noqa: BLE001 - any online failure triggers fallback
            if active_mode == "online" and self._mode == "auto":
                # Print the real reason Azure failed BEFORE silently
                # switching engines — without this, a fallback to
                # offline is completely invisible in the console, and
                # the only symptom is unexpectedly worse translation
                # quality with no clue why (see the "(Offline)" badge
                # in the UI for the same signal, less detail).
                print(f"[pipeline] Azure engine failed to start, falling back to offline: {exc!r}")
                connectivity.invalidate()  # whatever we believed about the network, re-check it next time
                self._offline_due_to_network = True
                if self._on_engine_change:
                    self._on_engine_change("connecting")
                engine = OfflineEngine()
                active_mode = "offline"
                try:
                    engine = self._start_engine_bounded(engine)
                except Exception as offline_exc:
                    # BOTH engines are down. Without this the offline
                    # error was the only one that reached the UI, so a
                    # missing AZURE_SPEECH_KEY presented as "offline
                    # engine isn't installed" — the wrong half of the
                    # story, and the reason this took so long to
                    # diagnose. Print both, and chain them so the
                    # traceback keeps the Azure cause too.
                    print(f"[pipeline] Offline engine ALSO failed to start: {offline_exc!r}")
                    print(f"[pipeline] Neither engine is available. Run: python scripts/diagnose.py")
                    raise offline_exc from exc
            else:
                raise

        if active_mode == "online":
            self._offline_due_to_network = False
            # Baseline for the watchdog's independent health check
            # (see ONLINE_HEALTH_PROBE_SECONDS) so it waits a full
            # interval before its first probe instead of firing right
            # on top of the connect check start_engine() just did.
            self._last_online_probe = time.monotonic()
            # Load the offline model now, in the background, while the
            # online engine is healthy — so that if the network drops
            # mid-sentence the fallback is instant instead of stalling
            # on a first-use model load at the worst possible moment.
            self._prewarm_offline()

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
        active. Each tick makes four independent checks:

          1. ENGINE — polls the mic stream for exceptions raised inside
             its audio callback (e.g. an AzureEngineError raised from
             feed()) and, in "auto" mode, restarts on the offline engine
             if the online one died mid-stream.
          2. HEALTH — while nominally online, independently re-verifies
             reachability every ONLINE_HEALTH_PROBE_SECONDS instead of
             waiting for AzureEngine to notice and report a failure
             itself. See that constant's comment for why step 1 alone
             isn't enough: a silent network death produces no feed()
             error to catch, for as long as Azure's SDK hasn't yet
             noticed either.
          3. RECONNECT — when the session is offline only because the
             network went away, re-probes periodically and climbs back
             to Azure once it's reachable again.
          4. DEVICE CHANGED — asks Windows (not PortAudio, which caches;
             see audio.refresh_devices) whether the default recording
             device is still the one this session opened. Catches the
             user switching mics in Sound settings, or plugging in a
             headset that Windows promotes to default automatically.
          5. DEVICE DIED — checks that audio is still physically
             arriving. Catches the case a changed-endpoint check can't:
             a mic yanked mid-sentence, where PortAudio raises nothing
             at all and the callbacks simply stop.

        The last two reopen the mic on the current default WITHOUT
        touching the engine, so the session keeps running and the user
        just carries on speaking into the new microphone.
        """
        while self._watchdog_running.is_set():
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            mic = self._mic
            if mic is None:
                continue

            # Checked before anything else, because every other branch
            # below needs an engine to be running and this is the only
            # one that can bring one back.
            if self._offline_retry_at is not None and self._engine is None:
                if time.monotonic() >= self._offline_retry_at:
                    print("[pipeline] retrying the offline engine now", flush=True)
                    self._offline_retry_at = None
                    self._restart_on_offline()
                continue

            error = mic.drain_errors()
            if error is not None:
                # Same visibility gap as _start_engine(): a mid-session
                # drop to offline is otherwise silent in the console.
                print(f"[pipeline] mic feed error while online, falling back to offline: {error!r}")
                if self._mode == "auto" and isinstance(self._engine, AzureEngine):
                    connectivity.invalidate()
                    self._restart_on_offline()
                continue

            if self._should_try_online_health_check():
                self._last_online_probe = time.monotonic()
                if not connectivity.is_online(force=True):
                    print(
                        "[pipeline] lost connectivity while online but Azure hasn't reported it yet; "
                        "falling back to offline",
                        flush=True,
                    )
                    self._restart_on_offline()
                continue

            if self._should_try_reconnect():
                self._try_restore_online()
                continue

            if not self._follow_default:
                continue  # user pinned a specific mic; never move them off it

            endpoint_id = audio.get_default_input_endpoint_id()
            if endpoint_id is not None and endpoint_id != self._last_endpoint_id:
                self._last_endpoint_id = endpoint_id
                print("[pipeline] Windows default recording device changed; reopening mic", flush=True)
                self._swap_microphone()
                continue

            if mic.seconds_since_audio() > AUDIO_STALL_SECONDS:
                print(
                    f"[pipeline] no audio for {mic.seconds_since_audio():.1f}s "
                    f"(device likely removed); reopening mic",
                    flush=True,
                )
                self._swap_microphone()

    def _should_try_online_health_check(self) -> bool:
        """
        _should_try_online_health_check()
        Usage: internal — True when the watchdog should spend a probe
        independently confirming the online engine is still actually
        reachable, rather than trusting that a failure would announce
        itself.

          * mode must be "auto" — an explicitly online session is the
            user overriding the connectivity-aware behavior on purpose,
            so it is left to sink or swim on Azure's own reporting.
          * the engine must currently be AzureEngine — nothing to
            double-check about an already-offline session here (that's
            _should_try_reconnect's job).
          * the session must not be paused — same reasoning as
            _should_try_reconnect: no audio is flowing, so there is
            nothing time-sensitive to protect by spending a probe now.
          * enough time must have passed since the last one.
        """
        if self._mode != "auto":
            return False
        if not isinstance(self._engine, AzureEngine):
            return False
        if self._paused:
            return False
        return (time.monotonic() - self._last_online_probe) >= ONLINE_HEALTH_PROBE_SECONDS

    def _should_try_reconnect(self) -> bool:
        """
        _should_try_reconnect()
        Usage: internal — True when the watchdog should spend a network
        probe on trying to get back online.

        Every condition here is a reason NOT to reconnect, and each one
        matters:
          * mode must be "auto" — an explicitly offline session stays
            offline, and an explicitly online one never fell back.
          * the drop must have been caused by the network, not by the
            user's plan or a deliberate setting.
          * the engine must currently be offline (nothing to restore
            otherwise).
          * the session must not be paused. Reconnecting while paused
            would open a connection and then feed it nothing, and Azure
            drops an idle connection — producing a cancellation that
            surfaces on resume as a fresh failure. Wait until there is
            actually audio to send.
          * enough time must have passed since the last probe.
        """
        if self._mode != "auto" or not self._offline_due_to_network:
            return False
        if not isinstance(self._engine, OfflineEngine):
            return False
        if self._paused:
            return False
        return (time.monotonic() - self._last_reconnect_probe) >= RECONNECT_PROBE_SECONDS

    def _try_restore_online(self) -> None:
        """
        _try_restore_online()
        Usage: internal — called from the watchdog when the session is
        running offline purely because the network was unavailable.
        Probes connectivity and, if it's back, swaps the engine to
        Azure without touching the mic stream, so the user keeps
        talking and the captions simply get better.

        The probe result is checked before anything is torn down. A
        failed swap would otherwise leave the session with no engine at
        all for as long as the offline one takes to restart — a much
        worse outcome than staying offline a little longer.
        """
        self._last_reconnect_probe = time.monotonic()
        if not connectivity.is_online(force=True):
            return

        print("[pipeline] network is back; restoring the online engine", flush=True)
        if self._on_engine_change:
            # Azure's own connect handshake (see AzureEngine._wait_for_connection)
            # can take a few seconds, so say a switch is underway rather
            # than leaving the "(Offline)" label up unexplained while it
            # runs — mirrors the "connecting" notice on the way down.
            self._on_engine_change("connecting")
        with self._lock:
            if not self._watchdog_running.is_set():
                return  # session ended while this was queued
            previous = self._engine
            try:
                engine = AzureEngine()
                engine.start(self._from_lang, self._to_lang, self._on_engine_result)
            except Exception as exc:  # noqa: BLE001 - must not kill the watchdog thread
                # Reachable but not usable — an expired key, a service
                # outage, a proxy that completes a TLS handshake and
                # then blocks the WebSocket. Keep the offline engine
                # running and try again after the next interval.
                print(f"[pipeline] could not restore online engine, staying offline: {exc!r}", flush=True)
                if self._on_engine_change:
                    # Undo the "connecting" notice above — the attempt
                    # failed and the offline engine is still the one
                    # actually running, so the badge must say so again
                    # rather than sitting on a transition that didn't
                    # happen.
                    self._on_engine_change("offline")
                return

            self._engine = engine
            self._offline_due_to_network = False
            self._last_online_probe = time.monotonic()
            if previous is not None:
                try:
                    previous.stop()
                except Exception as exc:  # noqa: BLE001 - the old engine is already replaced
                    print(f"[pipeline] error stopping the offline engine after recovery: {exc!r}", flush=True)

        if self._on_engine_change:
            self._on_engine_change("online")

    def _swap_microphone(self) -> None:
        """
        _swap_microphone()
        Usage: internal — called from the watchdog when the microphone
        the session is recording from is no longer the right one (the
        Windows default moved, or the device stopped responding).
        Closes the current stream, re-enumerates the hardware, and
        reopens on whatever the default now is. The ENGINE is
        deliberately left running throughout: a mic change is not a
        translation change, and tearing the engine down would drop the
        Azure connection and the recognizer's in-progress utterance for
        no reason.

        Order is not negotiable. The stream has to be fully closed
        before audio.refresh_devices() runs, because refreshing means
        terminating and re-initializing PortAudio, which aborts any open
        stream — and the refresh has to happen before the new default is
        resolved, because until it does, PortAudio is still reporting
        the device list it cached at startup and would hand back the
        very device that just went away.

        Retries a few times with a short pause: a device Windows has
        only just promoted to default is often not ready to be opened
        for a moment afterwards, and a wireless dongle reconnecting can
        take longer still. Reports the result through on_device_change
        either way, so the UI can show the new mic's name — or None,
        which is the app's only honest way of saying it currently has no
        working microphone at all.
        """
        with self._lock:
            if not self._watchdog_running.is_set():
                return  # session ended while this was queued; nothing to swap onto

            if self._mic is not None:
                self._mic.stop()
                self._mic = None

            audio.refresh_devices()

            last_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    mic = MicrophoneStream(
                        device_index=None,  # resolve the CURRENT default, not a stale index
                        on_chunk=self._feed_engine,
                        on_level=self._on_level,
                    )
                    mic.set_paused(self._paused)
                    mic.start()
                except Exception as exc:  # noqa: BLE001 - must not kill the watchdog thread
                    last_exc = exc
                    time.sleep(0.4 * (attempt + 1))
                    continue

                self._mic = mic
                device = audio.describe_default_input_device()
                name = device["name"] if device else "Unknown device"
                print(f"[pipeline] microphone reopened on: {name}", flush=True)
                if self._on_device_change:
                    self._on_device_change(device)
                return

            print(f"[pipeline] could not reopen any microphone: {last_exc!r}", flush=True)
            if self._on_device_change:
                self._on_device_change(None)

    def _restart_on_offline(self) -> None:
        """
        _restart_on_offline()
        Usage: internal — swaps the active engine to OfflineEngine
        without dropping the mic stream, so a lost internet connection
        mid-session degrades gracefully instead of going silent.

        Wrapped in a try/except because OfflineEngine.start() genuinely
        can fail (missing Whisper model directory or no Argos package
        for the pair — see scripts/prepare_offline_assets.py) — or hang
        (see OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS), which
        _start_engine_bounded() turns into the same kind of failure so
        this code doesn't need to treat them differently. This runs
        on the watchdog thread, so an uncaught exception here would kill
        that thread silently and leave the app with no engine at all and
        no indication of why. Report it as an engine change to "failed"
        instead, so the UI can say something.
        """
        if self._on_engine_change:
            # Fired immediately, before the mic is even paused-for below
            # or the model is loaded — this is the moment the drop was
            # DETECTED, not the moment the switch finishes, so the UI
            # reflects reality (mid-transition) for however long that
            # actually takes rather than showing stale "Listening…".
            self._on_engine_change("connecting")
        with self._lock:
            if not self._watchdog_running.is_set():
                return  # session ended while this was queued
            if self._engine is not None:
                self._engine.stop()
            self._engine = None

        # The start runs OUTSIDE self._lock on purpose. A cold offline
        # start can legitimately take tens of seconds (see
        # OFFLINE_ENGINE_COLD_START_TIMEOUT_SECONDS), and stop() takes
        # this same lock — so holding it across the load would mean the
        # Stop button, the Exit button and a mic hot-swap all hang for
        # the duration of a model load. The engine has already been
        # torn down above, so there is nothing here another thread can
        # corrupt in the meantime; the only state that needs the lock
        # back is installing the new engine, below.
        try:
            engine = self._start_engine_bounded(OfflineEngine())
        except Exception as exc:  # noqa: BLE001 - must not kill the watchdog thread
            # Arm a retry rather than giving up. The attempt that just
            # timed out is almost always still running in the
            # background finishing the work, and the retry will attach
            # to it and get the engine (see _offline_retry_at).
            self._offline_retry_at = time.monotonic() + OFFLINE_RETRY_AFTER_SECONDS
            print(f"[pipeline] offline fallback failed to start: {exc!r}", flush=True)
            print(f"[pipeline] retrying the offline engine in {OFFLINE_RETRY_AFTER_SECONDS:.0f}s", flush=True)
            if self._on_engine_change:
                self._on_engine_change("connecting")
            return

        with self._lock:
            if not self._watchdog_running.is_set():
                # The user stopped the session while the model was
                # loading. Shut the freshly-started engine down rather
                # than adopting it, or its flush thread would outlive
                # the session and keep transcribing into nothing.
                engine.stop()
                return
            self._engine = engine
            self._offline_retry_at = None  # we have an engine; nothing left to retry
            # Mark this as network-caused so the watchdog will try to
            # climb back to Azure once the connection returns. Set only
            # here, on the mid-session failure path — never when the
            # user or their plan chose offline deliberately.
            self._offline_due_to_network = True
            self._last_reconnect_probe = time.monotonic()
            if self._on_engine_change:
                self._on_engine_change("offline")

    def start(self, from_lang: str, to_lang: str, device_index: Optional[int], mode: EngineMode = "auto") -> None:
        """
        start(from_lang, to_lang, device_index, mode)
        Usage: begin a live translation session. from_lang/to_lang are UI
        display names, device_index is a value from audio.list_input_devices()
        (or None for the system default), mode forces "online" or
        "offline" or leaves it as "auto" (connectivity-aware: online
        when reachable, offline when not, switching either way as the
        network changes).
        """
        with self._lock:
            self._from_lang, self._to_lang = from_lang, to_lang
            self._device_index, self._mode = device_index, mode
            self._paused = False
            self._offline_due_to_network = False
            self._offline_retry_at = None
            self._last_reconnect_probe = time.monotonic()
            # device_index=None means "follow whatever Windows calls the
            # default"; anything else is a mic the user pinned on
            # purpose. Only the former may be hot-swapped by the
            # watchdog (see _watchdog_loop).
            self._follow_default = device_index is None
            # Re-enumerate before opening anything, so a session started
            # after a headset was plugged in actually sees that headset
            # rather than the device list cached when the app launched.
            audio.refresh_devices()
            # Baseline for change detection. Taken BEFORE the stream
            # opens: taken after, a device change during startup would
            # be recorded as the starting state and then never noticed.
            self._last_endpoint_id = audio.get_default_input_endpoint_id()

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

        The flag is also recorded on the pipeline itself, not just on
        the current stream, so that a microphone swapped in afterwards
        (see _swap_microphone) starts in the same state — otherwise
        changing mics while paused would quietly resume translating.

        Resuming also resets the reconnect probe clock, so a session
        that sat paused while offline gets its first chance to climb
        back to Azure promptly rather than waiting out an interval that
        elapsed while nothing was happening.
        """
        self._paused = paused
        if not paused:
            self._last_reconnect_probe = 0.0
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
            self._offline_due_to_network = False
            self._offline_retry_at = None
            # An engine parked by an abandoned start attempt owns a live
            # flush thread; ending the session has to stop it too, or it
            # outlives the session that caused it.
            if self._offline_unclaimed_engine is not None:
                try:
                    self._offline_unclaimed_engine.stop()
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    print(f"[pipeline] could not stop the unclaimed offline engine: {exc!r}", flush=True)
                self._offline_unclaimed_engine = None
                self._offline_unclaimed_pair = None
            if self._mic is not None:
                self._mic.stop()
                self._mic = None
            if self._engine is not None:
                self._engine.stop()
                self._engine = None
