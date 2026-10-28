CREATE TABLE IF NOT EXISTS analysis_context_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    analysis_context_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    UNIQUE(analysis_context_id, attempt_number),
                    FOREIGN KEY(analysis_context_id)
                        REFERENCES analysis_context_snapshots(analysis_context_id)
                );
CREATE TABLE IF NOT EXISTS analysis_context_snapshots (
                    analysis_context_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    trigger_count INTEGER NOT NULL,
                    context_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    analyzed_at TEXT,
                    last_error TEXT,
                    UNIQUE(cycle_id, capability_id),
                    FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
                );
CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                );
CREATE TABLE IF NOT EXISTS collection_watermark (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    checked_at TEXT NOT NULL
                );
CREATE TABLE IF NOT EXISTS cycle_evidence (
                    cycle_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, evidence_id),
                    FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
                );
CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    source_digest TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    experiment_id TEXT
                );
CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    hypothesis_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    candidate_id TEXT,
                    benchmark_run_id TEXT,
                    evaluation_plan_json TEXT,
                    decision_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    deletion_reason TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id),
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                );
CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    requires_human_selection INTEGER NOT NULL,
                    selected_at TEXT,
                    ignored_at TEXT,
                    ignore_reason TEXT,
                    deleted_at TEXT,
                    deletion_reason TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
                );
CREATE TABLE IF NOT EXISTS hypothesis_candidates (
                    hypothesis_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                );
CREATE TABLE IF NOT EXISTS promotion_bundles (
                    experiment_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                );
CREATE INDEX IF NOT EXISTS idx_analysis_context_attempt
                    ON analysis_context_attempts(analysis_context_id, attempt_number DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_context_cycle
                    ON analysis_context_snapshots(cycle_id, capability_id);
CREATE INDEX IF NOT EXISTS idx_cycle_evidence_id ON cycle_evidence(evidence_id);
CREATE INDEX IF NOT EXISTS idx_experiments_state ON experiments(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_hypotheses_cycle ON hypotheses(cycle_id, confidence DESC);
