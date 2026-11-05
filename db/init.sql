CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id  TEXT,
    src_ip      INET,
    surface     TEXT,            -- web | openai_api | ollama | mcp | vllm | lmstudio | litellm | llamacpp | openwebui | gradio
    method      TEXT,
    path        TEXT,
    status      SMALLINT,
    ua          TEXT,
    headers     JSONB,
    body        TEXT,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    severity    SMALLINT NOT NULL DEFAULT 0,  -- 0 info .. 3 high
    extra       JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_ip_idx   ON events (src_ip);
CREATE INDEX IF NOT EXISTS events_sid_idx  ON events (session_id);
CREATE INDEX IF NOT EXISTS events_tags_idx ON events USING GIN (tags);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,   -- user | assistant | tool
    content     TEXT,
    tool_name   TEXT,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    reviewed    BOOLEAN NOT NULL DEFAULT false   -- eğitime alınacak mı
);
CREATE INDEX IF NOT EXISTS messages_sid_idx ON messages (session_id, id);

CREATE OR REPLACE FUNCTION notify_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('new_event', NEW.id::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_notify ON events;
CREATE TRIGGER events_notify AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION notify_event();
