"""Copy a date range out of production, read-only, into a throwaway pgvector database.

Production is only ever read through ``docker exec <postgres> psql`` with
``default_transaction_read_only=on`` (checked before anything is exported). The
copy lands in a database of a separate container this module starts, bound to
127.0.0.1; its schema comes from this branch's migrations (``alembic upgrade
head``). Each prepared range is a template database; every replay run works in
a fresh copy of it, so runs never see one another's writes.

What a range ``[start, end]`` copies:

- every source;
- the events of ``[start - lookback, end]`` and the articles of ``[start, end]``
 (with their stored embeddings and triage results; bodies are cut to the
  excerpt triage reads);
- the published editions and items of ``[start - lookback, start)``, which the
  first replayed day's repeat screen compares with;
- read-only reference data for ``[start, end]`` (the editor ratings, edition
  items and duplicate-screen decisions production recorded), written as
  ``reference.json`` next to the template and never loaded as live rows.
"""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config

import daily_insights_api
from daily_insights_api.core.config import get_settings
from daily_insights_api.modules.newsroom.assembly import PUBLISHED_LOOKBACK_DAYS
from daily_insights_api.modules.newsroom.triage import BODY_EXCERPT_CHARS

ALEMBIC_INI = Path(daily_insights_api.__file__).resolve().parents[2] / "alembic.ini"
COPY_CHUNK = 1 << 20
SERVER_START_TIMEOUT = 60.0
# Production only; the throwaway server is plain Postgres with pgvector.
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
# Tables imported into the template, in foreign-key order.
IMPORTED_TABLES = (
    "newsroom_sources",
    "newsroom_events",
    "newsroom_articles",
    "newsroom_editions",
    "newsroom_edition_items",
)


@dataclass(frozen=True, slots=True)
class ReplayEnvironment:
    """Where a replay reads from, where it writes, and its throwaway database server."""

    root: Path
    source_container: str = "daily-insights-newsroom-postgres-1"
    keys_container: str = "daily-insights-newsroom-api-1"
    server_container: str = "newsroom-replay-pg"
    server_port: int = 45433
    server_image: str = "pgvector/pgvector:pg17"
    server_password: str = "replay"

    def conninfo(self, database: str = "postgres") -> str:
        return (
            f"host=127.0.0.1 port={self.server_port} user=postgres "
            f"password={self.server_password} dbname={database}"
        )

    def database_url(self, database: str) -> str:
        return (
            f"postgresql+psycopg://postgres:{self.server_password}"
            f"@127.0.0.1:{self.server_port}/{database}"
        )

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def snapshot_dir(self, snapshot: "Snapshot") -> Path:
        return self.root / "snapshots" / snapshot.name


@dataclass(frozen=True, slots=True)
class Snapshot:
    start: date
    end: date
    lookback_days: int = PUBLISHED_LOOKBACK_DAYS

    @property
    def name(self) -> str:
        return f"{self.start:%Y%m%d}-{self.end:%Y%m%d}"

    @property
    def template(self) -> str:
        return f"replay_{self.start:%Y%m%d}_{self.end:%Y%m%d}"

    @property
    def history_start(self) -> date:
        return self.start - timedelta(days=self.lookback_days)

    def dates(self) -> list[date]:
        return [
            self.start + timedelta(days=offset)
            for offset in range((self.end - self.start).days + 1)
        ]


class ReplayError(RuntimeError):
    """A replay precondition failed; the message says which."""


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


# --- Production, read-only ------------------------------------------------------------


class SourceDatabase:
    """Production Postgres, reached only through ``psql`` in its container, read-only."""

    def __init__(self, container: str) -> None:
        self._container = container
        self._user = self._printenv("POSTGRES_USER") or "postgres"
        self._database = self._printenv("POSTGRES_DB") or self._user

    def _printenv(self, name: str) -> str:
        result = _run(["docker", "exec", self._container, "printenv", name], check=False)
        return result.stdout.strip()

    def _psql(self, sql: str, *, tuples_only: bool) -> list[str]:
        return [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGOPTIONS={READ_ONLY_OPTIONS}",
            self._container,
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self._user,
            "-d",
            self._database,
            *(["-At"] if tuples_only else []),
            "-c",
            sql,
        ]

    def scalar(self, sql: str) -> str:
        result = _run(self._psql(sql, tuples_only=True), check=False)
        if result.returncode != 0:
            raise ReplayError(f"production query failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def assert_read_only(self) -> None:
        if self.scalar("SHOW default_transaction_read_only") != "on":
            raise ReplayError("the production session is not read-only; nothing was exported")

    def rows(self, sql: str) -> list[dict[str, Any]]:
        text = self.scalar(f"SELECT coalesce(json_agg(row_to_json(t)), '[]') FROM ({sql}) t")
        rows: list[dict[str, Any]] = json.loads(text)
        return rows

    def columns(self, table: str) -> list[str]:
        return self.scalar(
            "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
            "FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}'"
        ).split(",")

    @contextmanager
    def copy_out(self, sql: str) -> Iterator[subprocess.Popen[bytes]]:
        """CSV rows of ``sql`` streamed from production."""
        process = subprocess.Popen(
            self._psql(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv)", tuples_only=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            yield process
        finally:
            _, stderr = process.communicate()
            if process.returncode != 0:
                raise ReplayError(f"production export failed: {stderr.decode().strip()}")


# --- The throwaway server -------------------------------------------------------------


def ensure_server(environment: ReplayEnvironment) -> None:
    """Start the throwaway pgvector container (127.0.0.1 only) unless it already runs."""
    state = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", environment.server_container],
        check=False,
    )
    if state.returncode != 0:
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                environment.server_container,
                "-e",
                f"POSTGRES_PASSWORD={environment.server_password}",
                "-p",
                f"127.0.0.1:{environment.server_port}:5432",
                environment.server_image,
                # A throwaway copy: durability buys nothing here.
                "-c",
                "fsync=off",
                "-c",
                "synchronous_commit=off",
                "-c",
                "full_page_writes=off",
                "-c",
                "shared_buffers=512MB",
            ]
        )
    elif state.stdout.strip() != "true":
        _run(["docker", "start", environment.server_container])
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while True:
        try:
            with psycopg.connect(environment.conninfo(), connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def remove_server(environment: ReplayEnvironment) -> None:
    _run(["docker", "rm", "-f", environment.server_container], check=False)


def database_exists(environment: ReplayEnvironment, name: str) -> bool:
    with psycopg.connect(environment.conninfo(), autocommit=True) as connection:
        found = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    return found is not None


def drop_database(environment: ReplayEnvironment, name: str) -> None:
    with psycopg.connect(environment.conninfo(), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def create_database(environment: ReplayEnvironment, name: str, *, template: str | None) -> None:
    with psycopg.connect(environment.conninfo(), autocommit=True) as connection:
        connection.execute(
            f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")
        )


def migrate(environment: ReplayEnvironment, database: str) -> None:
    """``alembic upgrade head`` of this branch, aimed at ``database`` only."""
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = environment.database_url(database)
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


# --- Preparing a range ------------------------------------------------------------------


def _filters(snapshot: Snapshot) -> dict[str, str]:
    history, start, end = (
        snapshot.history_start.isoformat(),
        snapshot.start.isoformat(),
        snapshot.end.isoformat(),
    )
    earlier_editions = (
        f"SELECT id FROM newsroom_editions WHERE status = 'published' "
        f"AND edition_date >= '{history}' AND edition_date < '{start}'"
    )
    return {
        "newsroom_sources": "true",
        "newsroom_events": f"edition_date BETWEEN '{history}' AND '{end}'",
        "newsroom_articles": f"edition_date BETWEEN '{start}' AND '{end}'",
        "newsroom_editions": f"id IN ({earlier_editions})",
        "newsroom_edition_items": f"edition_id IN ({earlier_editions})",
    }


def _select_expression(table: str, column: str) -> str:
    if table == "newsroom_articles" and column == "body":
        return f"left(body, {BODY_EXCERPT_CHARS}) AS body"
    return f'"{column}"'


def _local_columns(connection: psycopg.Connection[Any], table: str) -> list[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def _latest_batch(table: str, where: str, partition: str) -> str:
    """Rows of the last assembly that wrote ``table`` per ``partition``.

    One assembly commits each screen's rows together, so they share ``created_at``.
    """
    return (
        f"SELECT * FROM (SELECT *, max(created_at) OVER (PARTITION BY {partition}) AS latest "
        f"FROM {table} WHERE {where}) t WHERE created_at = latest"
    )


def _reference(source: SourceDatabase, snapshot: Snapshot) -> dict[str, Any]:
    window = f"edition_date BETWEEN '{snapshot.start}' AND '{snapshot.end}'"
    return {
        "editor_ratings": source.rows(
            "SELECT edition_date, market_code, event_id, event_title, position, score, stars, "
            "picked, follow_up, prompt_version FROM ("
            + _latest_batch("newsroom_editor_ratings", window, "edition_date, market_code")
            + ") r ORDER BY edition_date, market_code, position"
        ),
        "dedupe_decisions": source.rows(
            "SELECT edition_date, scope, event_id, event_title, matched_event_id, matched_title, "
            "title_similarity, article_similarity, new_facts, outcome FROM ("
            + _latest_batch("newsroom_dedupe_decisions", window, "edition_date, scope")
            + ") d ORDER BY edition_date, scope, matched_event_id, event_id"
        ),
        "items": source.rows(
            "SELECT e.edition_date, e.market_code, e.selection_mode, i.event_id, i.rank, i.stars, "
            "i.editor_score, i.origin, i.removed_at IS NOT NULL AS removed, "
            "i.hidden_at IS NOT NULL AS hidden, ev.working_title "
            "FROM newsroom_edition_items i "
            "JOIN newsroom_editions e ON e.id = i.edition_id "
            "JOIN newsroom_events ev ON ev.id = i.event_id "
            f"WHERE e.{window} ORDER BY e.edition_date, e.market_code, i.rank"
        ),
    }


def prepare(
    environment: ReplayEnvironment, snapshot: Snapshot, *, refresh: bool = False
) -> dict[str, Any]:
    """Build (or reuse) the template database and reference data of ``snapshot``."""
    directory = environment.snapshot_dir(snapshot)
    metadata_path = directory / "snapshot.json"
    ensure_server(environment)
    if not refresh and metadata_path.exists() and database_exists(environment, snapshot.template):
        metadata: dict[str, Any] = json.loads(metadata_path.read_text(encoding="utf-8"))
        return metadata
    source = SourceDatabase(environment.source_container)
    source.assert_read_only()
    drop_database(environment, snapshot.template)
    create_database(environment, snapshot.template, template=None)
    migrate(environment, snapshot.template)
    counts: dict[str, int] = {}
    with psycopg.connect(environment.conninfo(snapshot.template)) as connection:
        # Migrations seed sources; production's rows replace them.
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'newsroom\\_%'"
            ).fetchall()
        ]
        connection.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
        # Foreign keys to users and other unexported tables are not checked.
        connection.execute("SET session_replication_role = replica")
        for table, where in _filters(snapshot).items():
            remote = set(source.columns(table))
            columns = [column for column in _local_columns(connection, table) if column in remote]
            select = ", ".join(_select_expression(table, column) for column in columns)
            quoted = ", ".join(f'"{column}"' for column in columns)
            with (
                source.copy_out(f"SELECT {select} FROM {table} WHERE {where}") as process,
                connection.cursor().copy(
                    f"COPY {table} ({quoted}) FROM STDIN WITH (FORMAT csv)"
                ) as copy,
            ):
                assert process.stdout is not None
                while chunk := process.stdout.read(COPY_CHUNK):
                    copy.write(chunk)
            counts[table] = int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]
        # What production decided, kept beside the copy for mapping replayed
        # events back (a triage replay reassigns every article).
        connection.execute(
            "CREATE TABLE replay_article_origin AS "
            "SELECT id AS article_id, event_id, edition_date FROM newsroom_articles"
        )
        connection.execute(
            "CREATE TABLE replay_prod_events AS SELECT id AS event_id, edition_date, "
            "working_title, headline_zh_hant, summary_zh_hant FROM newsroom_events"
        )
        connection.commit()
    with psycopg.connect(environment.conninfo(snapshot.template), autocommit=True) as connection:
        connection.execute("ANALYZE")
    reference = _reference(source, snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "reference.json").write_text(
        json.dumps(reference, ensure_ascii=False), encoding="utf-8"
    )
    metadata = {
        "start": snapshot.start.isoformat(),
        "end": snapshot.end.isoformat(),
        "history_start": snapshot.history_start.isoformat(),
        "template": snapshot.template,
        "exported_at": datetime.now(UTC).isoformat(),
        "source_revision": source.scalar("SELECT version_num FROM alembic_version"),
        "rows": counts,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def load_reference(environment: ReplayEnvironment, snapshot: Snapshot) -> dict[str, Any]:
    path = environment.snapshot_dir(snapshot) / "reference.json"
    if not path.exists():
        raise ReplayError(f"{snapshot.name} is not prepared; run the prepare command first")
    reference: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return reference


def read_provider_setting(environment: ReplayEnvironment, name: str) -> str | None:
    """One setting of the production API container, read at run time and kept in memory only."""
    result = _run(
        ["docker", "exec", environment.keys_container, "printenv", f"DAILY_INSIGHTS_{name}"],
        check=False,
    )
    value = result.stdout.strip()
    return value or None
