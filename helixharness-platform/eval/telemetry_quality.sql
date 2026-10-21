CREATE TABLE IF NOT EXISTS telemetry_documents (
                    source_path TEXT PRIMARY KEY,
                    source_root TEXT NOT NULL,
                    source_commit TEXT,
                    source_branch TEXT,
                    file_hash TEXT NOT NULL,
                    feature TEXT,
                    project_name TEXT,
                    document_schema TEXT NOT NULL DEFAULT 'unsupported',
                    publication_status TEXT,
                    finalization_status TEXT,
                    quality_status TEXT NOT NULL,
                    automation_eligible INTEGER NOT NULL,
                    issues_json TEXT NOT NULL,
                    last_updated TEXT,
                    ingested_at TEXT NOT NULL
                );
CREATE TABLE IF NOT EXISTS telemetry_entries (
                    source_path TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    helixharness_version TEXT,
                    command TEXT,
                    phase TEXT,
                    stage_kind TEXT,
                    status TEXT,
                    outcome_reason TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    session_id TEXT,
                    gate_result TEXT,
                    evidence_complete INTEGER,
                    admitted_at TEXT,
                    fixed_evidence_json TEXT NOT NULL DEFAULT '{}',
                    async_evidence_json TEXT NOT NULL DEFAULT '{}',
                    code_evidence_json TEXT NOT NULL DEFAULT '{}',
                    produced_files_json TEXT NOT NULL,
                    PRIMARY KEY (source_path, entry_id),
                    FOREIGN KEY (source_path) REFERENCES telemetry_documents(source_path) ON DELETE CASCADE
                );
CREATE INDEX IF NOT EXISTS idx_telemetry_documents_quality
                    ON telemetry_documents(quality_status, automation_eligible);
CREATE INDEX IF NOT EXISTS idx_telemetry_entries_version
                    ON telemetry_entries(helixharness_version, stage_kind, status);
