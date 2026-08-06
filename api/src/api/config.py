"""Api configuration. Twelve-factor: everything overridable via env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure placeholder signing secret. Fine for the no-auth simulator/CI default,
# but the api refuses to boot with auth on while this is still in place
# (see app.auth.assert_secure_auth_config).
DEFAULT_AUTH_SECRET = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="API_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080

    # CORS for clients served from a different origin (the Even Hub WebView, vite
    # dev). Comma-separated; "*" is allowed for local dev only. The bundled web UI
    # is same-origin, so production deployments usually don't need this at all.
    cors_origins: str = "*"

    # Directory holding the built web SPA, served statically by the api so one
    # container carries both the UI and the API (see api/Dockerfile). When the
    # directory doesn't exist (local dev, tests) nothing is mounted.
    web_dir: str = "/srv/web"

    # ---- LiteLLM gateway ---------------------------------------------------------
    # The OpenAI-compatible base URL + bearer key for the STT model: the api sends
    # /audio/transcriptions requests here and the gateway routes the model alias to
    # the backing server (litellm/config.yaml). The path component (/v1) is part of
    # the value. Point it at a model server's own /v1 to bypass the gateway. The key
    # is empty for an unauthenticated direct server; set it to the gateway's
    # master/virtual key, which rejects unauthenticated calls.
    litellm_endpoint: str = "http://litellm:4000/v1"
    litellm_api_key: str = ""

    # STT backend selector: "stub" (model-free, CI/simulator) or "parakeet" (an
    # OpenAI-compatible audio-transcription endpoint via the LiteLLM gateway). The
    # parakeet backend drives both partials (trailing-window re-decode on a cadence)
    # and finals (whole-turn decode) off the one HTTP model at stt_endpoint_url.
    stt_backend: str = "stub"
    # The model alias sent to the gateway (must match litellm/config.yaml).
    stt_model: str = "parakeet"

    # ---- direct STT route (XERK-115) --------------------------------------------
    # The caption hot path sends ONE transcription request per partial — several a
    # second, per session — so every hop on it is paid over and over. Routing those
    # through the LiteLLM gateway costs an extra proxy round trip plus the OpenAI
    # transform's json -> verbose_json rewrite, none of which the api needs: it
    # already knows exactly which model server it wants. Set this to the STT
    # server's own /v1 wherever the api can genuinely open a socket to it (the
    # single-host compose stack can) and captions skip the gateway entirely.
    #
    # Left empty — the default — both fall back to litellm_endpoint /
    # litellm_api_key, so a split-host deploy that only reaches the model through
    # the gateway keeps working untouched. Cues are unaffected either way; they
    # always go through the gateway.
    stt_endpoint: str = ""
    # Bearer key for the direct route. Usually empty: a model server behind the
    # gateway doesn't authenticate. Only read when stt_endpoint is set.
    stt_api_key: str = ""

    # ---- Cues (XERK-81) ----------------------------------------------------------
    # Cues are private contextual info cards the api derives from the live
    # conversation: someone asks how far the sun is and the answer appears above the
    # transcript. Generation runs on each finalized segment against a chat LLM served
    # through the SAME LiteLLM gateway (no new URL/key var — it reuses litellm_endpoint
    # + litellm_api_key, POSTing /chat/completions instead of /audio/transcriptions).
    #   "off"    — no cues (default; the stripped core stays STT-only unless enabled).
    #   "stub"   — model-free, deterministic generator for CI/dev (no GPU).
    #   "openai" — real chat model via the gateway (prod: gpt-oss:120b on Ollama,
    #   the tenir-ollama-cue container — see docs/cues.md).
    cue_backend: str = "off"  # off | stub | openai
    # The chat-model alias sent to the gateway for cue generation (matches the
    # LiteLLM route + the deployed API_LLM_MODEL). The gateway owns the real model id.
    llm_model: str = "gpt-oss:120b"
    # How much recent transcript (finalized turns) to feed the cue model as context.
    cue_context_segments: int = 8
    # Cap the model's cue body length (characters) so a cue fits the glasses box and
    # the live band; the generator prompt also asks for brevity.
    cue_max_body_chars: int = 240
    # Disable the chat model's chain-of-thought for cue calls. Reasoning models
    # (gpt-oss today; the retired Qwen3 that motivated this) can spend the whole
    # token budget on reasoning and return an empty `content`, so no cue is ever
    # produced. Cues want fast structured JSON, not reasoning, so default on. A
    # server that doesn't know the `enable_thinking` kwarg ignores/drops it
    # (LiteLLM's drop_params); turn off only for a chat template that rejects it.
    cue_disable_thinking: bool = True

    # ---- Live translations (XERK-160) --------------------------------------------
    # When a finalized turn's detected language isn't English, the session
    # translates it via the same gateway the cues use (translation_model over
    # litellm_endpoint) and delivers it as a `translation` WS message paired to the
    # segment. While a non-English run is live, cue generation is suppressed; when
    # the run ends (an English turn arrives, or speech goes quiet past the hold
    # window below) a `translation.done` message tells the glasses to start the
    # translation box's dismiss countdown, and cues resume.
    #   "off"    — no translations (default; matches the stripped core).
    #   "stub"   — model-free, deterministic translator for CI/dev (no GPU).
    #   "openai" — real chat model via the gateway (prod: gpt-oss:120b weights
    #   behind the dedicated translation alias below).
    translation_backend: str = "off"  # off | stub | openai
    # The chat-model alias sent to the gateway for translations (matches the
    # LiteLLM route + the deployed API_TRANSLATION_MODEL). Same gpt-oss:120b
    # weights as llm_model, but the alias carries reasoning_effort low: the
    # XERK-180 eval (scripts/translation_eval/RESULTS-2026-08.md) found low ~2x
    # faster than the cue route's medium with accuracy within noise, and every
    # dedicated smaller model an accuracy loss. Point this at llm_model's alias
    # to fall back to the shared route.
    translation_model: str = "gpt-oss:120b-translate"
    # How long speech may go quiet after the last non-English activity before the
    # run is declared done. Finals only land at pauses, so partial captions also
    # count as activity — the window only starts once the speaker actually stops.
    translation_hold_ms: int = 3000

    # ---- Music ID (XERK-184) -----------------------------------------------------
    # When a song is playing in the room, the session periodically fingerprints a
    # short window of the live audio, identifies the track, and shows its
    # time-synced lyrics in the cue box — auto-scrolling as the song plays. Unlike
    # cues/translations this is AUDIO-driven (a fingerprint, not the transcript),
    # so it taps on_audio and runs on its own scan loop. The synced lyrics come
    # from LRCLIB (music_lyrics_endpoint) keyed by the recognized artist/title.
    #   "off"    — no music ID (default; the stripped core stays STT-only).
    #   "stub"   — model-free, deterministic identifier for CI/dev (no network).
    #   "shazam" — real recognition via the shazamio library (Shazam's global
    #   catalog); best-effort and unofficial, so a paid provider can drop in
    #   behind the same seam later. See docs/music-id.md.
    music_backend: str = "off"  # off | stub | shazam
    # LRCLIB base URL for time-synced (LRC) lyrics — free, no key. Self-host and
    # point this at your own instance to keep lyric lookups on-premises.
    music_lyrics_endpoint: str = "https://lrclib.net"
    # How often (ms) to fingerprint the audio while SEARCHING for a song. Kept
    # conservative: each scan is a recognition call, and while nothing matches
    # there is no reason to hammer it (also rate-limit hygiene for shazamio).
    music_scan_interval_ms: int = 8000
    # Ceiling (ms) the searching scan decays to on repeated misses. A session with
    # no music should settle at this cadence and stay there — recognition calls are
    # rate-limited upstream (replaying recorded sessions at full tilt drew Shazam
    # 429s; XERK-187), so an idle hour must not spend hundreds of them.
    music_scan_max_interval_ms: int = 60000
    # How often (ms) to RE-identify once a song is locked — the drift-correcting
    # `song.sync`. Longer than the search interval: a locked song only needs its
    # anchor nudged, and the local client clock carries the scroll between syncs.
    music_lock_interval_ms: int = 18000
    # How long (ms) the song box lingers with no fresh match before the run is
    # declared done (`song.done`): covers a brief gap between tracks or a scan
    # that missed, without dropping the lyrics the instant one window fails.
    # Replaying real sessions (XERK-187) showed quiet passages miss 1–3
    # consecutive scans mid-song, so the hold must survive two failed lock-cadence
    # re-checks (music_lock_interval_ms apart) or the box flickers done/new.
    music_hold_ms: int = 45000
    # Minimum recognizer confidence (0..1) to open/keep a song run. Below it the
    # window is treated as "no song playing" and nothing is shown.
    music_min_confidence: float = 0.5
    # Hard ceiling (ms) on one recognition call. shazamio's HTTP stack retries
    # with no total timeout — replaying real sessions caught a single call taking
    # 10+ minutes on a flaky upstream (XERK-187), which would stall the scan loop
    # that long. Timing out raises like any transient failure: the scan counts a
    # miss and the next tick proceeds. Kept under music_lock_interval_ms so even
    # a locked song's re-check can't overrun its own cadence.
    music_identify_timeout_ms: int = 15000
    # Seconds of the most-recent audio to fingerprint per scan. Landmark
    # recognition is reliable from ~5s; more only sharpens the match.
    music_window_seconds: float = 8.0
    # Song-end prep (XERK-192). When a song has played to its end the box should
    # clear promptly instead of lingering on the last lyric until the no-match hold
    # (music_hold_ms) finally expires. The run carries the full synced lyrics and
    # (usually) the track duration, so the session knows when the track is over and
    # schedules a precise `song.done` for that moment. The hold still covers a song
    # CUT SHORT (recognition stops matching early); this covers one that finishes.
    #
    # The song-time position treated as "the track is over": the recognizer's
    # reported duration when it has one, else the last synced lyric line plus this
    # tail (so the final line stays readable a beat before the box clears). Unused
    # when a duration is known.
    music_end_tail_ms: int = 4000
    # How close (ms) to the song's end the scan tightens from the slow locked
    # cadence (music_lock_interval_ms) back to the search cadence
    # (music_scan_interval_ms), so the NEXT track is fingerprinted and shown quickly
    # the moment this one ends rather than after one more slow locked re-check.
    music_end_prep_ms: int = 20000

    # ---- Cue evidence retrieval (XERK-120) ---------------------------------------
    # The cue model's weights are frozen years back (measured on the previously
    # deployed Qwen3, which reported an October 2023 cutoff; gpt-oss is likewise
    # frozen), so cues about current events answered from memory are guesses. With retrieval on, the session gathers evidence — the RSS-fed news
    # corpus, Wikipedia, SearXNG web search — under a hard deadline and rides it in
    # the cue prompt; a cue whose fact came from evidence carries the source label
    # to the clients (the attribution line under the cue body).
    #   "off"  — no grounding (default; cues behave exactly as before XERK-120).
    #   "stub" — deterministic evidence for CI/dev, no network.
    #   "live" — news corpus + Wikipedia + SearXNG.
    cue_retrieval_backend: str = "off"  # off | stub | live
    # Hard deadline for the retrieval fan-out. Evidence that lands in time rides
    # the prompt; slower tiers settle into a per-session cache for the next cue
    # consideration instead of delaying this one.
    #
    # 2000ms, recalibrated for XERK-124: the original 800ms was measured against
    # an engine set that turned out to be dead, and the engines that actually
    # answer (google cse, duckduckgo, mojeek via SearXNG) take ~1.0-1.2s — so at
    # 800ms the web tier, the freshest general source we have, never rode the
    # first prompt at all; its results only ever landed via the topic cache. A
    # question gets exactly one cue attempt on the turn it is asked, so evidence
    # arriving "next time" is evidence never seen. 2000ms lets a working web
    # tier land; suspended engines fail in ~10ms, so a down tier still costs
    # nothing. (News FTS <10ms and Wikipedia ~300-400ms are unaffected.)
    cue_retrieval_deadline_ms: int = 2000
    # Cap on evidence snippets in the prompt. Measured on the deployed model:
    # ~1400 evidence tokens costs ~200ms on the ~1.1s call — cheap; 4x that is not.
    cue_retrieval_max_evidence: int = 6
    # Wikipedia API root for the encyclopedia tier ("" disables the tier).
    cue_wikipedia_endpoint: str = "https://en.wikipedia.org"
    # Local Kiwix (kiwix-serve) root holding a Wikipedia ZIM mirror, used as the
    # encyclopedia tier's FALLBACK when the live site fails ("" = no fallback).
    # Compared against live Wikipedia (XERK-124): 2-4x faster and immune to rate
    # limits, but the snapshot trails by weeks and its ranking is measurably
    # worse — so it backs up the live tier rather than replacing it.
    cue_kiwix_endpoint: str = ""
    # Kiwix book filter (books.filter.name). The default matches any English
    # Wikipedia ZIM regardless of flavour/date suffix, so a refreshed download
    # needs no config change.
    cue_kiwix_book: str = "wikipedia_en_all"
    # Self-hosted SearXNG root for the web tier ("" disables the tier — there is no
    # public default on purpose; point it at your own instance).
    cue_searxng_endpoint: str = ""
    # Engines pinned per request, comma-separated ("" = the instance's defaults).
    # Pinning is the latency lever: the measured default answers in ~500-800ms
    # where the full engine fan-out takes 1-1.5s, and it leaves the shared
    # instance's engine config untouched for its other consumers.
    #
    # Pin several *working* engines, for redundancy rather than for a winner
    # (XERK-124). The old pin was startpage + bing + duckduckgo: measured against
    # the deployed SearXNG, startpage answers only with a CAPTCHA and bing was
    # not even among the instance's enabled engines, so the web tier returned
    # zero usable results for every real query.
    #
    # Redundancy is the point. Every one of these engines rate-limits or
    # CAPTCHA-walls a self-hosted instance sooner or later, so a pin is only as
    # good as its spares: measured over four consecutive queries, a two-engine
    # pin answered 1 of 4 (whichever engine got asked twice suspended), while
    # these three answered 4 of 4 — when one suspends the others still carry it.
    # Engine reachability drifts, so re-probe rather than trusting this list.
    cue_searxng_engines: str = "google cse,duckduckgo,mojeek"
    # RSS feeds ingested into the news corpus (comma-separated URLs). Fetched every
    # cue_rss_interval_seconds while retrieval is "live"; items older than
    # cue_rss_keep_days are pruned so every corpus hit is recent by construction.
    cue_rss_feeds: str = (
        "https://feeds.bbci.co.uk/news/world/rss.xml,"
        "https://feeds.npr.org/1001/rss.xml,"
        "https://www.theguardian.com/world/rss"
    )
    cue_rss_interval_seconds: int = 600
    cue_rss_keep_days: int = 14

    # Realtime windowing / VAD. Tune for the latency budget.
    stt_partial_interval_ms: int = 700  # re-run the partial hypothesis this often
    # Partials decode only this trailing window of the in-flight segment so partial
    # latency stays bounded instead of growing with turn length; finals still decode
    # the whole segment for a stable transcript.
    stt_partial_window_ms: int = 6000
    # Turn-close windows, retuned for XERK-175. Translations and cues both run
    # only on FINALIZED turns, so every ms a turn stays open is added latency on
    # them: over the July 2026 deployment sessions (105 conversations, ~7.8 h of
    # retained audio replayed through this exact VAD), the previous 700/12000
    # left a spoken word waiting a mean 4.7 s (p90 9.6 s) for its final, and 20 %
    # of speech turns never found a pause and rode out the full 12 s cap.
    # 500/8000 cuts the mean wait to 3.0 s (p90 6.1 s) and, because many real
    # pauses fall between 500 and 700 ms, closes far more turns on an actual
    # pause instead of an arbitrary mid-speech chop (forced-cap closes drop to
    # 16 % even with the tighter cap; 8 % at 500/12000). Re-run
    # scripts/stt_eval/segment_sim.py against current session audio before
    # retuning further.
    stt_max_segment_ms: int = 8000  # force a final at this segment length
    stt_min_segment_ms: int = 400  # don't close a turn on silence below this length
    stt_silence_ms: int = 500  # trailing silence that closes a turn
    stt_silence_rms: float = 0.005  # energy threshold below which audio is "silent"
    # Adaptive VAD (XERK-115). stt_silence_rms alone is an ABSOLUTE gate: in a room
    # whose noise floor sits above it — a café, a car, a fan — no frame is ever
    # "silent", so no turn ever closes on a pause and every caption waits the full
    # stt_max_segment_ms. With this on, the absolute value becomes a floor and the
    # real threshold tracks the measured background level, so pauses are found in
    # a noisy room and behaviour in a quiet one is unchanged.
    stt_vad_adaptive: bool = True
    # How far above the tracked noise floor a frame must sit to count as speech.
    # ~3x ≈ +10 dB — enough headroom that room noise doesn't read as speech, low
    # enough that a quiet talker still does.
    stt_vad_noise_ratio: float = 3.0
    # Trailing audio the noise floor is measured over. Long enough to span a normal
    # inter-word gap (so the quietest frame in it really is background, not a
    # mid-sentence stop), short enough to follow a room that changes.
    stt_vad_window_ms: int = 3000
    # LocalAgreement-2 (XERK-90): commit a partial's words only once two consecutive
    # window hypotheses agree, so live captions grow word by word instead of
    # rewriting the whole line each cadence. Off falls back to the raw-window partial.
    stt_local_agreement: bool = True
    # Ask the offline engine for per-word timestamps on FINAL decodes. Off by
    # default: measured on the deployed Parakeet server, word-timestamp computation
    # is ~550 ms of an ~675 ms final decode (~5.5x; 122 ms without — see
    # scripts/stt_eval/RESULTS-2026-07.md), it delays every final caption a user
    # sees, and nothing consumes the words today: no client reads
    # CaptionFinal.words, and stored segment timing (start/end_ms) is derived from
    # the audio byte count, not from word timestamps. Flip on only if a consumer
    # of per-word timing ships.
    stt_final_word_timestamps: bool = False

    # Fallback household scope. The household normally comes from the authenticated
    # principal's token (auth is always on, see auth_* below); this is only the
    # default seeded into the bootstrap admin.
    household_id: str = "default"

    # Persistence + full-audio retention. The conversation transcript store and the
    # audio store each sit behind a seam: "memory" keeps everything in-process
    # (simulator/CI default), the real backends use Postgres + local disk (install
    # '.[persistence]' for Postgres). "off" disables persistence entirely (live
    # captions still work, nothing is retained).
    persistence_backend: str = "memory"  # memory | postgres | off
    audio_backend: str = "memory"  # memory | disk | off
    # Root directory for the "disk" audio backend. Bind-mount this in compose so
    # retained audio survives container recreation.
    audio_dir: str = "/data/audio"

    # Multi-user auth & household tenancy. Auth is always on: login issues a signed
    # bearer token and every REST/WS call is scoped to the user's household by that
    # token. There is no no-login mode.
    # HMAC signing secret for bearer tokens. MUST be overridden in production; the
    # api refuses to boot while this is still the insecure default.
    auth_secret: str = DEFAULT_AUTH_SECRET
    # Bearer-token lifetime. Tokens slide (XERK-168): any authenticated request past
    # half a token's life gets a fresh one back in the X-Renewed-Token header, which
    # the clients adopt — so this is effectively the max *idle* time before a device
    # is asked to log in again, not a hard re-login cadence.
    auth_token_ttl_seconds: int = 2_592_000  # 30 days
    # Grace window a dropped session stays resumable: a reconnect carrying the same
    # sessionId rebinds to the live session instead of starting a fresh one.
    # 0 disables resume.
    session_resume_grace_seconds: int = 30
    # Optional bootstrap admin so a fresh instance is usable: when set, this user is
    # created in the user store at startup so there is someone who can log in.
    auth_admin_username: str = ""
    auth_admin_password: str = ""
    auth_admin_household: str = "default"

    # Infra endpoints. The host matches the Postgres service name in
    # docker-compose.yml (compose also sets this var explicitly, but the default
    # must resolve for anyone relying on it).
    database_url: str = "postgresql://tenir:tenir@postgres-tenir:5432/tenir"
    # Optional explicit path to schema.sql. Left empty, the Postgres store locates
    # it (the image ships it at the workdir; dev/test resolve the repo-root file)
    # and applies it idempotently on pool open, so an existing data dir gains
    # additively-added tables/indexes — Postgres only runs schema.sql on a FRESH
    # volume, so a DB created before a new table (e.g. `cues`) never gets it.
    schema_path: str = ""

    # ---- component status dashboard (see api.status) ------------------------
    # A background loop probes each real backend's health endpoint and caches the
    # result for GET /status, so the api can surface red/yellow/green per component.
    status_probe_interval_seconds: float = 5.0
    status_probe_timeout_seconds: float = 3.0
    # Grace window for an unreachable component before it goes red. vLLM servers
    # refuse connections until their model has loaded (which can take minutes), so a
    # freshly-unreachable component shows yellow ("connecting") for this long before
    # settling to red ("down") — a loading model goes yellow→green, a dead one →red.
    status_grace_seconds: float = 180.0
    # Direct health-probe URL for the STT model server. Empty — the default — means
    # "the api has no direct route to this server", so its light mirrors the one
    # LiteLLM gateway probe: the status page then follows the same path the api's
    # real traffic takes. Set it only where the api can genuinely open a socket to
    # the server (the single-host compose stack does).
    status_stt_url: str = ""
    # Direct health-probe URL for the cue LLM server, same contract as
    # status_stt_url: empty means the cue light mirrors the gateway probe (the api
    # reaches the model only through LiteLLM), and a value is used only where the api
    # can open a socket straight to the model server.
    status_llm_url: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def stt_endpoint_url(self) -> str:
        """The OpenAI-compatible base URL the caption path posts audio to: the direct
        STT route when one is configured, else the LiteLLM gateway."""
        return self.stt_endpoint or self.litellm_endpoint

    @property
    def stt_key(self) -> str:
        """The bearer key that goes with :attr:`stt_endpoint_url`. A direct route
        carries its own (usually absent) key rather than the gateway's master key —
        sending the gateway key to a model server would just be a stray header."""
        return self.stt_api_key if self.stt_endpoint else self.litellm_api_key

    @property
    def litellm_probe_url(self) -> str:
        """The LiteLLM gateway root for health probes. The gateway serves
        /health/liveliness off its root, not its /v1 base, so derive the root from
        litellm_endpoint (dropping a trailing /v1) instead of carrying a second var —
        the status probe then always follows the same gateway the api sends requests to."""
        base = self.litellm_endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base


settings = Settings()
