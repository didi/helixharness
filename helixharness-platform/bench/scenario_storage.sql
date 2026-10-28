CREATE TABLE IF NOT EXISTS scenario_calculations (
                    calculation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    contract_hash TEXT NOT NULL DEFAULT '',
                    contract_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, sequence_number),
                    FOREIGN KEY (run_id) REFERENCES scenario_runs(run_id)
                );
CREATE TABLE IF NOT EXISTS scenario_hard_gate_analyses (
                    analysis_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    calculation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES scenario_runs(run_id),
                    FOREIGN KEY (calculation_id) REFERENCES scenario_calculations(calculation_id)
                );
CREATE TABLE IF NOT EXISTS scenario_job_attempt_evidence (
                    job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    disposition TEXT NOT NULL,
                    terminal_status TEXT NOT NULL,
                    started_at_ms INTEGER,
                    completed_at_ms INTEGER,
                    duration_ms INTEGER,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    evidence_complete INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, attempt_number),
                    FOREIGN KEY (job_id) REFERENCES scenario_jobs(job_id)
                );
CREATE TABLE IF NOT EXISTS scenario_job_turn_evidence (
                    job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    turn_sequence INTEGER NOT NULL,
                    turn_id TEXT,
                    input_kind TEXT NOT NULL,
                    input_text TEXT,
                    workflow_step_id TEXT,
                    milestone_id TEXT,
                    classification TEXT NOT NULL,
                    started_at_ms INTEGER,
                    completed_at_ms INTEGER,
                    duration_ms INTEGER,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    final_response TEXT,
                    error_json TEXT,
                    events_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, attempt_number, turn_sequence),
                    FOREIGN KEY (job_id) REFERENCES scenario_jobs(job_id)
                );
CREATE TABLE IF NOT EXISTS scenario_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 0,
                    transient_failure_count INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    outcome_json TEXT,
                    workflow_progress_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE (run_id, case_id, side),
                    FOREIGN KEY (run_id, case_id)
                        REFERENCES scenario_run_cases(run_id, case_id)
                );
CREATE TABLE IF NOT EXISTS scenario_remediation_projections (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    hypothesis_id TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES scenario_runs(run_id)
                );
CREATE TABLE IF NOT EXISTS scenario_reports (
                    calculation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (calculation_id) REFERENCES scenario_calculations(calculation_id),
                    FOREIGN KEY (run_id) REFERENCES scenario_runs(run_id)
                );
CREATE TABLE IF NOT EXISTS scenario_run_cases (
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, case_id),
                    FOREIGN KEY (run_id) REFERENCES scenario_runs(run_id)
                );
CREATE TABLE IF NOT EXISTS scenario_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    run_kind TEXT NOT NULL,
                    execution_adapter TEXT NOT NULL,
                    workload_kind TEXT NOT NULL DEFAULT 'FORMAL_EVALUATION',
                    contract_json TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
                    preview_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    current_calculation_id TEXT,
                    error TEXT,
                    deleted_at TEXT
                );
CREATE TABLE IF NOT EXISTS scenario_versions (
                    case_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    case_kind TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    publication_status TEXT NOT NULL,
                    calibration_state TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY (case_id, source_hash)
                );
CREATE TABLE IF NOT EXISTS scenario_worker_status (
                    worker_id TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    workload_kind TEXT NOT NULL DEFAULT 'FORMAL_EVALUATION',
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE IF NOT EXISTS scenarios (
                    case_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    case_kind TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    publication_status TEXT NOT NULL,
                    calibration_state TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    FOREIGN KEY (case_id, source_hash)
                        REFERENCES scenario_versions(case_id, source_hash)
                );
CREATE INDEX IF NOT EXISTS scenario_hard_gate_analyses_run
                    ON scenario_hard_gate_analyses(run_id, created_at, analysis_id);
CREATE INDEX IF NOT EXISTS scenario_job_attempt_evidence_order
                    ON scenario_job_attempt_evidence(job_id, attempt_number);
CREATE INDEX IF NOT EXISTS scenario_job_turn_evidence_order
                    ON scenario_job_turn_evidence(job_id, attempt_number, turn_sequence);
CREATE INDEX IF NOT EXISTS scenario_jobs_queue
                    ON scenario_jobs(status, sequence_number);
CREATE INDEX IF NOT EXISTS scenario_run_cases_version_history
                    ON scenario_run_cases(case_id, source_hash, run_id);
CREATE INDEX IF NOT EXISTS scenario_runs_workload_queue ON scenario_runs(workload_kind, execution_adapter, status, created_at, run_id);
CREATE INDEX IF NOT EXISTS scenario_worker_status_adapter
                    ON scenario_worker_status(adapter, updated_at);
CREATE INDEX IF NOT EXISTS scenario_worker_status_workload ON scenario_worker_status(adapter, workload_kind, updated_at);
