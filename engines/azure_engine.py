"""
engines/azure_engine.py

Online translation engine using Azure Cognitive Services Speech
Translation. Does streaming speech recognition and translation in one
API call, which is why it's the primary engine — no separate
STT-then-translate round trip.

Requires AZURE_SPEECH_KEY plus either AZURE_SPEECH_REGION or
AZURE_SPEECH_ENDPOINT (see config.py / .env).

The important change here is that start() no longer returns
optimistically. It used to call start_continuous_recognition() and
return immediately — but that method only kicks off a session locally;
it does not wait for, or even require, a connection to Azure. So
pipeline.py would report "online", the UI would paint the globe green,
and only a second or two later would a cancellation arrive and flip
everything to offline. That gap is exactly the symptom of "it says
connected, then goes offline for no reason": both states were being
reported truthfully, just in the wrong order, because the first one was
a guess.

Now start() opens the connection explicitly and waits for Azure to
confirm it before returning. If Azure refuses or the network swallows
it, start() raises, the pipeline falls back once, and the UI only ever
sees the engine that actually works.

Usage:
    engine = AzureEngine()
    engine.start("English", "Persian (Farsi)", on_result=my_callback)
    engine.feed(pcm16_bytes)   # call repeatedly as audio arrives
    engine.stop()
"""

import threading

import azure.cognitiveservices.speech as speechsdk

from config import settings
from languages import to_azure_speech_locale, to_azure_target

from .base import ResultCallback, SpeechTranslator, TranslationResult

# How long start() waits for Azure to confirm the connection before
# giving up and letting the pipeline fall back. Generous enough for a
# slow link or a cold DNS cache, short enough that a user who clicked
# Resume on a dead network isn't left staring at a frozen button.
# connectivity.py has already probed reachability by this point, so
# reaching this timeout means Azure specifically is the problem, not
# the network in general.
CONNECT_TIMEOUT_SECONDS = 8.0


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
        self._connection: speechsdk.Connection | None = None
        self._to_lang_display: str = ""
        # Set by the SDK's `canceled` handler, re-raised from feed().
        # See _on_canceled() in start() for why it can't just raise.
        self._pending_error: Exception | None = None
        # Signalled by the SDK's `connected` event. start() blocks on
        # this so "online" is reported only once Azure has said so.
        self._connected = threading.Event()
        # Set when a cancellation arrives DURING the connect wait, so
        # start() can fail immediately with Azure's own reason instead
        # of sitting out the full timeout for an answer already given.
        self._connect_failure: str | None = None

    def _build_config(self) -> speechsdk.translation.SpeechTranslationConfig:
        """
        _build_config()
        Usage: internal — assembles the SpeechTranslationConfig using
        whichever of the two auth modes is configured.

        Region-based auth is right for a classic Speech resource. A
        resource with a custom subdomain — which every Azure AI
        Services / AI Foundry multi-service resource has, and which an
        84-character non-hex key indicates — rejects region-based auth
        with 401 no matter how valid the key is. Set
        AZURE_SPEECH_ENDPOINT to the value on the resource's "Keys and
        Endpoint" page to take the endpoint branch.
        """
        if not settings.azure_configured:
            raise AzureEngineError("Azure Speech key/region not configured")

        if settings.azure_speech_endpoint:
            return speechsdk.translation.SpeechTranslationConfig(
                subscription=settings.azure_speech_key,
                endpoint=settings.azure_speech_endpoint,
            )
        return speechsdk.translation.SpeechTranslationConfig(
            subscription=settings.azure_speech_key,
            region=settings.azure_speech_region,
        )

    def start(self, from_lang: str, to_lang: str, on_result: ResultCallback) -> None:
        """
        start(from_lang, to_lang, on_result)
        Usage: see SpeechTranslator.start. Raises AzureEngineError if
        credentials are missing, if Azure rejects them, or if the
        connection can't be established within CONNECT_TIMEOUT_SECONDS —
        the pipeline catches this and falls back to the offline engine.

        Unlike the previous version, this does not return until the
        connection is confirmed. See the module docstring for why that
        matters: returning early meant the "online" badge was a
        prediction rather than a fact, and it was frequently wrong.
        """
        self._to_lang_display = to_lang
        self._connected.clear()
        self._connect_failure = None

        speech_config = self._build_config()
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
            """
            Connection dropped / auth failed mid-session.

            This runs on the Speech SDK's OWN event thread. Raising here
            does not propagate anywhere — the SDK swallows the exception
            — which is why losing the network used to produce permanent
            silence: Azure canceled, nothing observed it, pipeline.py's
            watchdog never saw an error, and no fallback to the offline
            engine ever happened.

            So record it instead and let feed() re-raise it on the audio
            callback thread, whose exceptions MicrophoneStream queues up
            for the watchdog to drain. It's also printed here and now,
            because deferring the message to feed() means a cancellation
            that lands while the session is paused produces no output
            anywhere until the user resumes — and then appears with no
            hint that it had been sitting there the whole time.
            """
            if evt.reason == speechsdk.CancellationReason.EndOfStream:
                return  # normal end of the push stream during stop(), not a failure
            detail = f"{evt.reason} — code={getattr(evt, 'error_code', '?')} details={evt.error_details}"
            print(f"[azure] recognition canceled: {detail}", flush=True)
            self._pending_error = AzureEngineError(f"Azure recognition canceled: {detail}")
            # Unblocks a start() still waiting on the connect handshake,
            # so an auth rejection fails fast with Azure's own words
            # rather than timing out anonymously.
            self._connect_failure = detail
            self._connected.set()

        def _on_connected(evt) -> None:
            """Azure accepted the WebSocket — the only proof we're really online."""
            self._connected.set()

        self._recognizer.recognizing.connect(_on_recognizing)
        self._recognizer.recognized.connect(_on_recognized)
        self._recognizer.canceled.connect(_on_canceled)

        self._wait_for_connection()
        self._recognizer.start_continuous_recognition()

    def _wait_for_connection(self) -> None:
        """
        _wait_for_connection()
        Usage: internal — opens the connection to Azure ahead of
        recognition and blocks until it's confirmed, raising
        AzureEngineError if it isn't.

        Connection.open(for_continuous_recognition=True) is what makes
        this possible: without it the SDK connects lazily, on the first
        audio it's given, so there'd be nothing to wait for at start
        time and the engine could only discover a bad key after the
        user had already started speaking.

        If the SDK in use doesn't expose Connection at all, this
        degrades to the old optimistic behaviour rather than refusing
        to run — an older SDK should mean a less precise badge, not a
        dead app.
        """
        try:
            self._connection = speechsdk.Connection.from_recognizer(self._recognizer)
        except Exception as exc:  # noqa: BLE001 - older/limited SDK build
            print(f"[azure] connection verification unavailable on this SDK ({exc!r}); proceeding unverified", flush=True)
            return

        self._connection.connected.connect(lambda evt: self._connected.set())
        self._connection.open(True)

        if not self._connected.wait(CONNECT_TIMEOUT_SECONDS):
            raise AzureEngineError(
                f"Azure did not confirm a connection within {CONNECT_TIMEOUT_SECONDS:.0f}s "
                f"(host unreachable, or blocked by a proxy/firewall)"
            )
        if self._connect_failure is not None:
            # Azure answered during the handshake, and the answer was no.
            # Clear the pending error: it has done its job of failing
            # this start(), and leaving it set would make the NEXT
            # engine's first feed() raise someone else's error.
            failure, self._connect_failure, self._pending_error = self._connect_failure, None, None
            raise AzureEngineError(f"Azure refused the connection: {failure}")

    def feed(self, pcm16_chunk: bytes) -> None:
        """
        feed(pcm16_chunk)
        Usage: see SpeechTranslator.feed. Writes straight into the Speech
        SDK's push stream; a no-op if start() hasn't been called yet.

        Also the point where a cancellation captured by _on_canceled()
        gets re-raised. It has to surface here rather than at the SDK
        callback: this runs on the audio callback thread, and
        MicrophoneStream wraps its on_chunk call in a try/except that
        queues the exception for pipeline.py's watchdog to pick up and
        fall back to the offline engine. Cleared as it's raised so one
        cancellation triggers exactly one fallback.
        """
        if self._pending_error is not None:
            error, self._pending_error = self._pending_error, None
            raise error
        if self._push_stream is not None:
            self._push_stream.write(pcm16_chunk)

    def stop(self) -> None:
        """
        stop()
        Usage: see SpeechTranslator.stop. Stops continuous recognition and
        closes the push stream so the SDK's background threads exit.

        Each teardown step is independent: a recognizer whose connection
        already died can throw on stop, and letting that propagate would
        skip closing the push stream and leak the SDK's threads for the
        rest of the app's life — during a fallback, which is precisely
        when the app needs to stay healthy.
        """
        for label, action in (
            ("recognizer", lambda: self._recognizer and self._recognizer.stop_continuous_recognition()),
            ("connection", lambda: self._connection and self._connection.close()),
            ("push stream", lambda: self._push_stream and self._push_stream.close()),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - teardown of a possibly-dead session
                print(f"[azure] error closing {label} (session likely already dead): {exc!r}", flush=True)

        self._recognizer = None
        self._connection = None
        self._push_stream = None
        self._pending_error = None  # don't let a cancellation from this session surface later
        self._connect_failure = None
        self._connected.clear()
