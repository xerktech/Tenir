-- tenir Postgres schema.
--
-- The on-disk shape behind the SqlConversationStore and the SQL user store.
-- Applied by docker-compose.yml on the Postgres data dir's first boot.

-- A household is the sharing boundary. Conversations are scoped to it; users
-- authenticate into it.
CREATE TABLE IF NOT EXISTS households (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the implicit single-tenant household so persistence inserts work before
-- any login flow creates one. Matches the default API_HOUSEHOLD_ID; the
-- auth/seed-admin flow creates additional households as needed.
INSERT INTO households (id) VALUES ('default') ON CONFLICT (id) DO NOTHING;

-- Household members who can authenticate. The built-in auth service issues a
-- signed bearer token scoping every request to `household`; `role` gates
-- admin-only controls (user management, /metrics).
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household      TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    username       TEXT NOT NULL UNIQUE,
    role           TEXT NOT NULL DEFAULT 'member',     -- member | admin
    password_hash  TEXT NOT NULL,                      -- pbkdf2_sha256$...
    -- Marks the single env-managed bootstrap admin (API_AUTH_ADMIN_*). Reconciled
    -- from env on every boot by its stable id, not its (mutable) username.
    is_env_admin   BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_household_idx ON users (household);
-- At most one env-managed admin.
CREATE UNIQUE INDEX IF NOT EXISTS users_one_env_admin_idx ON users (is_env_admin) WHERE is_env_admin;

-- A persisted conversation: one live session's durable record.
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,                  -- = the session id
    household   TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    mic_source  TEXT,
    source_lang TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'live',       -- live | ready
    audio_key   TEXT,                               -- audio-store key for the WAV
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversations_household_started_idx
    ON conversations (household, started_at DESC);

-- One finalized transcript turn (mirrors the caption.final contract).
CREATE TABLE IF NOT EXISTS segments (
    segment_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    lang            TEXT
);
CREATE INDEX IF NOT EXISTS segments_conversation_idx ON segments (conversation_id, start_ms);

-- Keyword search over transcripts (SqlConversationStore.search): a functional
-- full-text index per segment, matched per-row with websearch_to_tsquery.
CREATE INDEX IF NOT EXISTS segments_fts_idx
    ON segments USING GIN (to_tsvector('simple', text));

-- One private context cue (mirrors the cue contract, XERK-81). Derived from the
-- conversation but not part of it; at_ms is its transcript-timeline position so
-- history renders it inline where it appeared. Deliberately NOT indexed for FTS:
-- cues are private context, kept out of the transcript search corpus.
CREATE TABLE IF NOT EXISTS cues (
    cue_id          TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    at_ms           INTEGER NOT NULL,
    -- Live-source attribution (XERK-120): the short label of the source the
    -- cue's fact was grounded in ("BBC News", "Wikipedia"). NULL for a cue from
    -- the model's own knowledge.
    source          TEXT
);
-- Additive column for data dirs created before XERK-120 (this file is applied
-- idempotently on every pool open; CREATE TABLE IF NOT EXISTS alone would skip
-- the new column on an existing table).
ALTER TABLE cues ADD COLUMN IF NOT EXISTS source TEXT;
CREATE INDEX IF NOT EXISTS cues_conversation_idx ON cues (conversation_id, at_ms);

-- Recent news items ingested from the configured RSS feeds (XERK-120): the
-- knowledge corpus the cue retrieval layer searches so cue facts about current
-- events are grounded in fresh reporting instead of the cue model's (years-
-- stale) training data. Searched with the same functional-GIN FTS pattern as
-- the transcript search; rows are pruned after API_CUE_RSS_KEEP_DAYS, so the
-- corpus stays small and every hit is recent by construction. Household-free on
-- purpose — public news, not user data.
CREATE TABLE IF NOT EXISTS news_items (
    id           TEXT PRIMARY KEY,       -- feed entry GUID (or link fallback)
    source       TEXT NOT NULL,          -- short feed label, e.g. "BBC News"
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,          -- entry description, tags stripped
    link         TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS news_items_published_idx ON news_items (published_at DESC);
CREATE INDEX IF NOT EXISTS news_items_fts_idx
    ON news_items USING GIN (to_tsvector('simple', title || ' ' || summary));
