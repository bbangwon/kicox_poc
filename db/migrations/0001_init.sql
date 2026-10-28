-- 전처리 산출물 적재 스키마 (docs/DB_LOADING_PLAN.md §4)

CREATE TABLE documents (
    id             text PRIMARY KEY,                 -- doc_id (PDF SHA-256 앞 12자)
    regulation_key text NOT NULL,                    -- alio:{idx} 또는 manual:{doc_id}
    name           text NOT NULL,
    amended_on     date,
    status         text NOT NULL DEFAULT 'LOADED'
                   CHECK (status IN ('LOADED', 'INDEXED', 'SUPERSEDED', 'REPEALED')),
    is_current     boolean NOT NULL DEFAULT false,
    summary        text NOT NULL,
    not_for        text,
    keywords       text[] NOT NULL DEFAULT '{}',
    questions      text[] NOT NULL DEFAULT '{}',
    content_hash   text NOT NULL,                    -- 적재한 산출물 묶음 해시
    indexed_hash   text                              -- catalog 포인트 입력 해시
);
CREATE UNIQUE INDEX documents_current_uq ON documents (regulation_key) WHERE is_current;

CREATE TABLE sections (
    id           uuid PRIMARY KEY,
    document_id  text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    ordinal      int NOT NULL,
    title        text NOT NULL,
    page_start   int,
    page_end     int,
    covers       text,
    not_covers   text,
    keywords     text[] NOT NULL DEFAULT '{}',
    questions    text[] NOT NULL DEFAULT '{}',
    searchable   boolean NOT NULL,
    content_hash text NOT NULL,
    indexed_hash text
);
CREATE INDEX sections_document_idx ON sections (document_id, ordinal);

CREATE TABLE chunks (
    id           uuid PRIMARY KEY,
    document_id  text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    section_id   uuid NOT NULL REFERENCES sections (id) ON DELETE CASCADE,
    ordinal      int NOT NULL,
    header       text NOT NULL,
    body         text NOT NULL,
    tokens       int NOT NULL,
    page_start   int,
    page_end     int,
    searchable   boolean NOT NULL,
    has_numbers  boolean NOT NULL,
    verified     boolean NOT NULL DEFAULT false,
    content_hash text NOT NULL,
    indexed_hash text
);
CREATE INDEX chunks_document_idx ON chunks (document_id, ordinal);
CREATE INDEX chunks_section_idx ON chunks (section_id);

CREATE TABLE tools (
    id          uuid PRIMARY KEY,
    document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    section_id  uuid NOT NULL REFERENCES sections (id) ON DELETE CASCADE,
    name        text NOT NULL,
    spec        jsonb NOT NULL,
    UNIQUE (document_id, name)
);

CREATE TABLE document_tools (
    document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    tool_id     uuid NOT NULL REFERENCES tools (id) ON DELETE CASCADE,
    added_by    text NOT NULL DEFAULT 'ingest' CHECK (added_by IN ('ingest', 'manual')),
    PRIMARY KEY (document_id, tool_id)
);
