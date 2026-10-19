"""Drop the legacy daily-news tables after their history moved to newsroom.

Revision ID: 20261001_0035
Revises: 20261001_0034

The legacy pipeline is gone (docs/specs/newsroom-pipeline.md §9). Its edition
history was copied into the newsroom tables by 20261001_0034; everything else
(candidates, prepared items, audits, workflows, checkpoints and provider
cooldowns) only served the removed code. Orchestration runs of the removed
``news_*`` functions and jobs that never finished are cancelled so no worker
keeps retrying a function it has no handler for; finished runs stay as history.

The downgrade recreates the tables empty, exactly as they were at
20261001_0034 (including the immutability triggers); dropped rows and the
cancelled runs are not restored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_0035"
down_revision: str | None = "20261001_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TABLES = (
    "news_candidate_publications",
    "prepared_news_items",
    "news_presentations",
    "news_candidates",
    "news_items",
    "news_generation_audits",
    "news_editions",
    "news_candidate_batches",
    "news_checkpoints",
    "news_workflows",
    "news_dependency_states",
)
LEGACY_FUNCTION_KEYS = (
    "news_global_refresh",
    "news_tw_equity_refresh",
    "news_us_equity_refresh",
    "news_publish",
)
CANCELLED_ERROR = "legacy_news_removed"

# ``pg_dump --schema-only`` of the tables at 20261001_0034.
_LEGACY_SCHEMA = (
    """
        CREATE TABLE news_candidate_batches (
            function_attempt_id uuid NOT NULL,
            edition_date date NOT NULL,
            market_code character varying(50) NOT NULL,
            status character varying(20) DEFAULT 'collecting'::character varying NOT NULL,
            input_digest character varying(64),
            source_as_of timestamp with time zone,
            result jsonb,
            id uuid NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            finalized_at timestamp with time zone,
            CONSTRAINT ck_news_candidate_batches_input_digest_sha256 CHECK (((input_digest IS
                NULL) OR (char_length((input_digest)::text) = 64))),
            CONSTRAINT ck_news_candidate_batches_market_code_valid CHECK (((market_code)::text =
                ANY ((ARRAY['global'::character varying, 'tw_equity'::character varying,
                'us_equity'::character varying])::text[]))),
            CONSTRAINT ck_news_candidate_batches_status_valid CHECK (((status)::text = ANY
                ((ARRAY['collecting'::character varying, 'ready'::character varying,
                'partial'::character varying, 'unavailable'::character varying, 'failed'::character
                varying, 'cancelled'::character varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_candidate_publications (
            publish_job_run_id uuid NOT NULL,
            candidate_id uuid NOT NULL,
            item_id uuid NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL
        )
    """,
    """
        CREATE TABLE news_candidates (
            id uuid NOT NULL,
            edition_id uuid,
            candidate_id character varying(64) NOT NULL,
            source_name character varying(100) NOT NULL,
            hostname character varying(255) NOT NULL,
            url text NOT NULL,
            headline character varying(1000) NOT NULL,
            seen_at timestamp with time zone,
            source_published_at timestamp with time zone,
            content_digest character varying(64),
            stage character varying(20) NOT NULL,
            drop_reason character varying(30),
            ai_rank integer,
            ai_topic character varying(50),
            ai_market character varying(20),
            ai_importance integer,
            ai_event_key character varying(80),
            item_id uuid,
            publish_run_id uuid,
            publish_requested_at timestamp with time zone,
            publish_requested_by_user_id uuid,
            publish_error character varying(500),
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            batch_id uuid,
            CONSTRAINT ck_news_candidates_drop_reason_valid CHECK (((drop_reason IS NULL) OR
                ((drop_reason)::text = ANY ((ARRAY['off_market'::character varying,
                'policy'::character varying, 'duplicate_event'::character varying,
                'summary_failed'::character varying, 'translation_failed'::character varying,
                'reserve'::character varying])::text[])))),
            CONSTRAINT ck_news_candidates_exactly_one_owner CHECK (((edition_id IS NULL) <>
                (batch_id IS NULL))),
            CONSTRAINT ck_news_candidates_stage_valid CHECK (((stage)::text = ANY
                ((ARRAY['discovered'::character varying, 'fetch_failed'::character varying,
                'unused'::character varying, 'reviewed'::character varying, 'prepared'::character
                varying, 'dropped'::character varying, 'published'::character varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_checkpoints (
            workflow_id uuid NOT NULL,
            key character varying(64) NOT NULL,
            stage character varying(30) NOT NULL,
            result jsonb,
            failure jsonb,
            repairs integer NOT NULL,
            expires_at timestamp with time zone NOT NULL,
            id uuid NOT NULL
        )
    """,
    """
        CREATE TABLE news_dependency_states (
            scope character varying(300) NOT NULL,
            state character varying(30) NOT NULL,
            failure jsonb,
            available_at timestamp with time zone,
            probe_run_id uuid,
            failures_count integer DEFAULT 0 NOT NULL,
            newest_article_at timestamp with time zone,
            updated_at timestamp with time zone DEFAULT now() NOT NULL
        )
    """,
    """
        CREATE TABLE news_editions (
            id uuid NOT NULL,
            edition_date date NOT NULL,
            revision integer NOT NULL,
            input_digest character varying(64) NOT NULL,
            derivation_version character varying(100) NOT NULL,
            model_name character varying(200),
            prompt_version character varying(100) NOT NULL,
            status character varying(20) NOT NULL,
            generated_at timestamp with time zone DEFAULT now() NOT NULL,
            caveat character varying(1000),
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            market_code character varying(50) DEFAULT 'global'::character varying NOT NULL,
            candidate_batch_id uuid,
            publication_job_run_id uuid,
            CONSTRAINT ck_news_editions_input_digest_sha256 CHECK
                ((char_length((input_digest)::text) = 64)),
            CONSTRAINT ck_news_editions_market_code_valid CHECK (((market_code)::text = ANY
                ((ARRAY['global'::character varying, 'tw_equity'::character varying,
                'us_equity'::character varying])::text[]))),
            CONSTRAINT ck_news_editions_revision_positive CHECK ((revision > 0)),
            CONSTRAINT ck_news_editions_status_valid CHECK (((status)::text = ANY
                ((ARRAY['complete'::character varying, 'partial'::character varying,
                'unavailable'::character varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_generation_audits (
            id uuid NOT NULL,
            edition_id uuid,
            stage character varying(50) NOT NULL,
            locale character varying(10),
            provider character varying(100) NOT NULL,
            model character varying(200) NOT NULL,
            prompt_version character varying(100) NOT NULL,
            input_digest character varying(64) NOT NULL,
            status character varying(20) NOT NULL,
            provider_request_id character varying(255),
            input_tokens integer,
            output_tokens integer,
            latency_ms integer,
            error_code character varying(100),
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            candidate_batch_id uuid,
            CONSTRAINT ck_news_generation_audits_exactly_one_owner CHECK (((edition_id IS NULL)
                <> (candidate_batch_id IS NULL))),
            CONSTRAINT ck_news_generation_audits_input_digest_sha256 CHECK
                ((char_length((input_digest)::text) = 64)),
            CONSTRAINT ck_news_generation_audits_status_valid CHECK (((status)::text = ANY
                ((ARRAY['succeeded'::character varying, 'failed'::character varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_items (
            id uuid NOT NULL,
            edition_id uuid NOT NULL,
            rank integer NOT NULL,
            topic character varying(50) NOT NULL,
            source_name character varying(100) NOT NULL,
            source_hostname character varying(255) NOT NULL,
            source_url text NOT NULL,
            source_headline character varying(1000) NOT NULL,
            source_published_at timestamp with time zone,
            importance integer NOT NULL,
            content_digest character varying(64) NOT NULL,
            numeric_facts jsonb NOT NULL,
            market character varying(20),
            event_key character varying(80),
            origin character varying(10) DEFAULT 'model'::character varying NOT NULL,
            hidden_at timestamp with time zone,
            hidden_by_user_id uuid,
            published_by_user_id uuid,
            CONSTRAINT ck_news_items_content_digest_sha256 CHECK
                ((char_length((content_digest)::text) = 64)),
            CONSTRAINT ck_news_items_importance_range CHECK (((importance >= 1) AND (importance
                <= 5))),
            CONSTRAINT ck_news_items_market_valid CHECK (((market IS NULL) OR ((market)::text =
                ANY ((ARRAY['global'::character varying, 'us'::character varying, 'asia'::character
                varying, 'china'::character varying, 'taiwan'::character varying,
                'europe'::character varying, 'commodities'::character varying, 'crypto'::character
                varying])::text[])))),
            CONSTRAINT ck_news_items_origin_valid CHECK (((origin)::text = ANY
                ((ARRAY['model'::character varying, 'manual'::character varying])::text[]))),
            CONSTRAINT ck_news_items_rank_positive CHECK ((rank > 0)),
            CONSTRAINT ck_news_items_topic_valid CHECK (((topic)::text = ANY
                ((ARRAY['markets'::character varying, 'economy'::character varying,
                'companies'::character varying, 'policy'::character varying, 'technology'::character
                varying, 'commodities'::character varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_presentations (
            item_id uuid NOT NULL,
            locale character varying(10) NOT NULL,
            headline character varying(1000) NOT NULL,
            summary text NOT NULL,
            CONSTRAINT ck_news_presentations_locale_valid CHECK (((locale)::text = ANY
                ((ARRAY['zh-hant'::character varying, 'zh-hans'::character varying, 'en'::character
                varying])::text[])))
        )
    """,
    """
        CREATE TABLE news_workflows (
            root_run_id uuid NOT NULL,
            run_id uuid NOT NULL,
            edition_date date NOT NULL,
            market_code character varying(50) NOT NULL,
            state character varying(30) NOT NULL,
            stage character varying(30) NOT NULL,
            progress jsonb NOT NULL,
            failures jsonb NOT NULL,
            attempt integer NOT NULL,
            next_retry_at timestamp with time zone,
            updated_at timestamp with time zone DEFAULT now() NOT NULL,
            id uuid NOT NULL
        )
    """,
    """
        CREATE TABLE prepared_news_items (
            batch_id uuid NOT NULL,
            candidate_id uuid NOT NULL,
            rank integer NOT NULL,
            topic character varying(50) NOT NULL,
            importance integer NOT NULL,
            market character varying(20),
            event_key character varying(80),
            numeric_facts jsonb NOT NULL,
            presentations jsonb NOT NULL,
            content_digest character varying(64) NOT NULL,
            id uuid NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT ck_prepared_news_items_content_digest_sha256 CHECK
                ((char_length((content_digest)::text) = 64)),
            CONSTRAINT ck_prepared_news_items_rank_positive CHECK ((rank > 0))
        )
    """,
    """
        ALTER TABLE ONLY news_candidate_batches
            ADD CONSTRAINT pk_news_candidate_batches PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_candidate_publications
            ADD CONSTRAINT pk_news_candidate_publications PRIMARY KEY (publish_job_run_id,
                candidate_id)
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT pk_news_candidates PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_checkpoints
            ADD CONSTRAINT pk_news_checkpoints PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_dependency_states
            ADD CONSTRAINT pk_news_dependency_states PRIMARY KEY (scope)
    """,
    """
        ALTER TABLE ONLY news_editions
            ADD CONSTRAINT pk_news_editions PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_generation_audits
            ADD CONSTRAINT pk_news_generation_audits PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT pk_news_items PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_presentations
            ADD CONSTRAINT pk_news_presentations PRIMARY KEY (item_id, locale)
    """,
    """
        ALTER TABLE ONLY news_workflows
            ADD CONSTRAINT pk_news_workflows PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY prepared_news_items
            ADD CONSTRAINT pk_prepared_news_items PRIMARY KEY (id)
    """,
    """
        ALTER TABLE ONLY news_candidate_batches
            ADD CONSTRAINT uq_news_candidate_batch_attempt UNIQUE (function_attempt_id)
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT uq_news_candidate_edition UNIQUE (edition_id, candidate_id)
    """,
    """
        ALTER TABLE ONLY news_checkpoints
            ADD CONSTRAINT uq_news_checkpoint_workflow_key UNIQUE (workflow_id, key)
    """,
    """
        ALTER TABLE ONLY news_editions
            ADD CONSTRAINT uq_news_edition_version UNIQUE (edition_date, market_code, revision)
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT uq_news_item_rank UNIQUE (edition_id, rank)
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT uq_news_item_url UNIQUE (edition_id, source_url)
    """,
    """
        ALTER TABLE ONLY news_workflows
            ADD CONSTRAINT uq_news_workflow_root_market UNIQUE (root_run_id, market_code)
    """,
    """
        ALTER TABLE ONLY prepared_news_items
            ADD CONSTRAINT uq_prepared_news_item_candidate UNIQUE (batch_id, candidate_id)
    """,
    """
        ALTER TABLE ONLY prepared_news_items
            ADD CONSTRAINT uq_prepared_news_item_rank UNIQUE (batch_id, rank)
    """,
    """
        CREATE INDEX ix_news_candidate_batches_market_date ON news_candidate_batches USING btree
            (market_code, edition_date, created_at)
    """,
    """
        CREATE INDEX ix_news_candidates_batch_id ON news_candidates USING btree (batch_id)
    """,
    """
        CREATE INDEX ix_news_candidates_edition_id ON news_candidates USING btree (edition_id)
    """,
    """
        CREATE INDEX ix_news_checkpoints_expires_at ON news_checkpoints USING btree (expires_at)
    """,
    """
        CREATE INDEX ix_news_editions_latest ON news_editions USING btree (edition_date,
            market_code, revision)
    """,
    """
        CREATE INDEX ix_news_generation_audits_edition ON news_generation_audits USING btree
            (edition_id, created_at)
    """,
    """
        CREATE INDEX ix_news_items_edition_id ON news_items USING btree (edition_id)
    """,
    """
        CREATE INDEX ix_news_workflows_market_date ON news_workflows USING btree (market_code,
            edition_date)
    """,
    """
        CREATE UNIQUE INDEX uq_news_candidate_batch ON news_candidates USING btree (batch_id,
            candidate_id) WHERE (batch_id IS NOT NULL)
    """,
    """
        CREATE TRIGGER news_candidate_publications_are_immutable BEFORE DELETE OR UPDATE ON
            news_candidate_publications FOR EACH ROW EXECUTE FUNCTION
            reject_orchestration_fact_mutation()
    """,
    """
        CREATE TRIGGER prepared_news_items_are_immutable BEFORE DELETE OR UPDATE ON
            prepared_news_items FOR EACH ROW EXECUTE FUNCTION reject_orchestration_fact_mutation()
    """,
    """
        ALTER TABLE ONLY news_candidate_batches
            ADD CONSTRAINT fk_news_candidate_batches_function_attempt_id_function_attempts
                FOREIGN KEY (function_attempt_id) REFERENCES function_attempts(id) ON DELETE
                RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidate_publications
            ADD CONSTRAINT fk_news_candidate_publications_candidate_id_news_candidates FOREIGN
                KEY (candidate_id) REFERENCES news_candidates(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidate_publications
            ADD CONSTRAINT fk_news_candidate_publications_item_id_news_items FOREIGN KEY
                (item_id) REFERENCES news_items(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidate_publications
            ADD CONSTRAINT fk_news_candidate_publications_publish_job_run_id_job_runs FOREIGN
                KEY (publish_job_run_id) REFERENCES job_runs(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT fk_news_candidates_batch_id_news_candidate_batches FOREIGN KEY
                (batch_id) REFERENCES news_candidate_batches(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT fk_news_candidates_edition_id_news_editions FOREIGN KEY (edition_id)
                REFERENCES news_editions(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT fk_news_candidates_item_id_news_items FOREIGN KEY (item_id)
                REFERENCES news_items(id) ON DELETE SET NULL
    """,
    """
        ALTER TABLE ONLY news_candidates
            ADD CONSTRAINT fk_news_candidates_publish_requested_by_user_id_users FOREIGN KEY
                (publish_requested_by_user_id) REFERENCES users(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_checkpoints
            ADD CONSTRAINT fk_news_checkpoints_workflow_id_news_workflows FOREIGN KEY
                (workflow_id) REFERENCES news_workflows(id) ON DELETE CASCADE
    """,
    """
        ALTER TABLE ONLY news_editions
            ADD CONSTRAINT fk_news_editions_candidate_batch_id_news_candidate_batches FOREIGN
                KEY (candidate_batch_id) REFERENCES news_candidate_batches(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_editions
            ADD CONSTRAINT fk_news_editions_publication_job_run_id_job_runs FOREIGN KEY
                (publication_job_run_id) REFERENCES job_runs(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_generation_audits
            ADD CONSTRAINT fk_news_generation_audits_candidate_batch_id_news_candi_d907 FOREIGN
                KEY (candidate_batch_id) REFERENCES news_candidate_batches(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_generation_audits
            ADD CONSTRAINT fk_news_generation_audits_edition_id_news_editions FOREIGN KEY
                (edition_id) REFERENCES news_editions(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT fk_news_items_edition_id_news_editions FOREIGN KEY (edition_id)
                REFERENCES news_editions(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT fk_news_items_hidden_by_user_id_users FOREIGN KEY (hidden_by_user_id)
                REFERENCES users(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_items
            ADD CONSTRAINT fk_news_items_published_by_user_id_users FOREIGN KEY
                (published_by_user_id) REFERENCES users(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY news_presentations
            ADD CONSTRAINT fk_news_presentations_item_id_news_items FOREIGN KEY (item_id)
                REFERENCES news_items(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY prepared_news_items
            ADD CONSTRAINT fk_prepared_news_items_batch_id_news_candidate_batches FOREIGN KEY
                (batch_id) REFERENCES news_candidate_batches(id) ON DELETE RESTRICT
    """,
    """
        ALTER TABLE ONLY prepared_news_items
            ADD CONSTRAINT fk_prepared_news_items_candidate_id_news_candidates FOREIGN KEY
                (candidate_id) REFERENCES news_candidates(id) ON DELETE RESTRICT
    """,
)


def upgrade() -> None:
    functions = ", ".join(f"'{key}'" for key in LEGACY_FUNCTION_KEYS)
    active_functions = (
        "SELECT id FROM function_runs "
        f"WHERE function_key IN ({functions}) "
        "AND status IN ('pending', 'running', 'retry_wait')"
    )
    op.execute(
        "UPDATE function_attempts SET status = 'cancelled', finished_at = now(), "
        f"error_code = '{CANCELLED_ERROR}' "
        f"WHERE status = 'running' AND function_run_id IN ({active_functions})"
    )
    op.execute(
        "UPDATE function_runs SET status = 'cancelled', completed_at = now(), "
        "next_attempt_at = NULL, lease_owner = NULL, lease_token = NULL, "
        f"lease_expires_at = NULL, error = '{CANCELLED_ERROR}' "
        f"WHERE id IN ({active_functions})"
    )
    # Jobs made only of the removed functions end with them; a mixed job such
    # as internal_services_daily_update is settled by the worker's reconcile.
    op.execute(
        "UPDATE job_runs SET status = 'cancelled', completed_at = now(), "
        "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
        "WHERE job_key LIKE 'news\\_%' AND status IN ('pending', 'running')"
    )
    op.execute(f"DROP TABLE {', '.join(LEGACY_TABLES)}")


def downgrade() -> None:
    for statement in _LEGACY_SCHEMA:
        op.execute(statement)
