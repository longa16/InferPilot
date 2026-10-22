ALTER TABLE inference_events ADD COLUMN provider_prompt_ms double precision;
ALTER TABLE inference_events ADD COLUMN provider_queue_ms double precision;
ALTER TABLE inference_events ADD COLUMN provider_completion_ms double precision;
ALTER TABLE inference_events ADD COLUMN provider_tps double precision;
