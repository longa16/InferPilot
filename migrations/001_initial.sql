CREATE TABLE applications (
 id uuid PRIMARY KEY, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 gateway_key_hash text UNIQUE NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 is_active boolean NOT NULL DEFAULT true, corpus_version text NOT NULL
);
CREATE TABLE benchmark_runs (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES applications ON DELETE CASCADE,
 strategy text NOT NULL, dataset_version text NOT NULL, configuration_json jsonb NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 status text NOT NULL, notes text, report_json jsonb
);
CREATE UNIQUE INDEX one_running_benchmark ON benchmark_runs ((true)) WHERE status IN ('running','cancel_requested');
CREATE TABLE inference_events (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES applications ON DELETE CASCADE,
 request_id uuid UNIQUE NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 provider text NOT NULL DEFAULT 'groq', model text NOT NULL, status text NOT NULL,
 http_status int, duration_ms double precision CHECK(duration_ms >= 0), ttft_ms double precision CHECK(ttft_ms >= 0),
 input_tokens bigint CHECK(input_tokens >= 0), output_tokens bigint CHECK(output_tokens >= 0),
 token_source text NOT NULL DEFAULT 'unavailable', estimated_cost_usd numeric(24,12),
 cost_source text NOT NULL DEFAULT 'unavailable', pricing_version text,
 cache_hit boolean NOT NULL DEFAULT false, stream boolean NOT NULL, error_type text,
 request_fingerprint text NOT NULL, benchmark_run_id uuid REFERENCES benchmark_runs ON DELETE CASCADE,
 avoided_input_tokens bigint, avoided_output_tokens bigint
);
CREATE INDEX event_app_time ON inference_events(app_id,started_at DESC,id DESC);
CREATE INDEX event_app_model_time ON inference_events(app_id,model,started_at DESC);
CREATE INDEX event_benchmark ON inference_events(benchmark_run_id);
CREATE TABLE benchmark_cases (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES benchmark_runs ON DELETE CASCADE,
 event_id uuid REFERENCES inference_events ON DELETE SET NULL,
 case_id text NOT NULL, scenario text NOT NULL, phase text NOT NULL,
 expected_answer_facts jsonb NOT NULL, actual_response_digest text, quality_pass boolean,
 duration_ms double precision, cost_usd numeric(24,12), cache_hit boolean NOT NULL DEFAULT false,
 UNIQUE(run_id,phase,case_id)
);
CREATE TABLE agent_runs (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES applications ON DELETE CASCADE,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, status text NOT NULL,
 input_window_json jsonb NOT NULL, findings_json jsonb NOT NULL DEFAULT '[]',
 tool_calls_json jsonb NOT NULL DEFAULT '[]', recommendation_json jsonb, evidence_json jsonb
);
CREATE TABLE cache_entries (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES applications ON DELETE CASCADE,
 key_hash text UNIQUE NOT NULL, response_json jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL, corpus_version text NOT NULL
);
CREATE INDEX cache_expiry ON cache_entries(expires_at);
