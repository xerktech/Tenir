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

    # STT backend selector: "stub" (model-free, CI/simulator) or "voxtral" (an
    # OpenAI-compatible audio-transcription endpoint via the LiteLLM gateway).
    stt_backend: str = "stub"
    # The model alias sent to the gateway (must match litellm/config.yaml).
    stt_model: str = "voxtral"

    # Realtime windowing / VAD. Tune for the latency budget.
    stt_partial_interval_ms: int = 700  # re-run the partial hypothesis this often
    # Partials decode only this trailing window of the in-flight segment so partial
    # latency stays bounded instead of growing with turn length; finals still decode
    # the whole segment for a stable transcript.
    stt_partial_window_ms: int = 6000
    stt_max_segment_ms: int = 12000  # force a final at this segment length
    stt_min_segment_ms: int = 400  # don't close a turn on silence below this length
    stt_silence_ms: int = 700  # trailing silence that closes a turn
    stt_silence_rms: float = 0.005  # energy threshold below which audio is "silent"
    # LocalAgreement-2 (XERK-90): commit a partial's words only once two consecutive
    # window hypotheses agree, so live captions grow word by word instead of
    # rewriting the whole line each cadence. Off falls back to the raw-window partial.
    stt_local_agreement: bool = True

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
    auth_token_ttl_seconds: int = 86400  # 24h
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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
