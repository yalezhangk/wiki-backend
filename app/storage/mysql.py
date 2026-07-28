from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.config import settings
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.ingest import IngestJobResponse, IngestValidation
from app.schemas.query import CitationResponse
from app.schemas.publish import PublicationResponse, PublishJobResponse, PublishStatusResponse


class StorageError(RuntimeError):
    """Raised when a storage operation fails."""


class ChatNotFoundError(StorageError):
    """Raised when a chat cannot be found."""


class StorageUnavailableError(StorageError):
    """Raised when MySQL is unavailable or misconfigured."""


class MySQLStorage:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database

    def initialize(self) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chats (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '会话数字自增主键',
                        title VARCHAR(200) NOT NULL COMMENT '会话标题',
                        status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '会话状态',
                        created_at DATETIME NOT NULL COMMENT '创建时间（UTC）',
                        updated_at DATETIME NOT NULL COMMENT '最后更新时间（UTC）',
                        last_message_at DATETIME NULL COMMENT '最后一条消息时间（UTC）'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='聊天会话表'
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '消息自增主键',
                        chat_id BIGINT UNSIGNED NOT NULL COMMENT '所属会话数字ID',
                        role VARCHAR(16) NOT NULL COMMENT '消息角色：user或assistant',
                        content TEXT NOT NULL COMMENT '消息正文',
                        sources JSON NOT NULL COMMENT '回答引用来源列表（JSON）',
                        relevant_pages JSON NOT NULL COMMENT '查询命中的Wiki页面列表（JSON）',
                        citations JSON NOT NULL COMMENT '结构化Wiki引用列表（JSON）',
                        created_at DATETIME NOT NULL COMMENT '创建时间（UTC）',
                        synthesis_path VARCHAR(500) NULL COMMENT '该助手消息保存成的Synthesis相对路径',
                        synthesized_at DATETIME NULL COMMENT '保存为Synthesis的时间（UTC）',
                        CONSTRAINT fk_chat_messages_chat_id
                            FOREIGN KEY (chat_id) REFERENCES chats(id)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='聊天消息表'
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_jobs (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
                        status VARCHAR(32) NOT NULL,
                        stage VARCHAR(32) NOT NULL DEFAULT 'uploaded',
                        progress_percent TINYINT UNSIGNED NOT NULL DEFAULT 0,
                        original_filename VARCHAR(255) NOT NULL,
                        stored_filename VARCHAR(255) NOT NULL,
                        source_path VARCHAR(500) NOT NULL,
                        created_pages JSON NOT NULL,
                        updated_pages JSON NOT NULL,
                        contradictions JSON NOT NULL,
                        validation JSON NOT NULL,
                        error TEXT NULL,
                        created_at DATETIME NOT NULL,
                        started_at DATETIME NULL,
                        updated_at DATETIME NOT NULL,
                        finished_at DATETIME NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_jobs (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
                        status VARCHAR(16) NOT NULL,
                        trigger_kind VARCHAR(16) NOT NULL,
                        scheduled_at DATETIME NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        started_at DATETIME NULL,
                        finished_at DATETIME NULL,
                        published_at DATETIME NULL,
                        release_id VARCHAR(64) NULL,
                        error TEXT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_changes (
                        id BIGINT PRIMARY KEY AUTO_INCREMENT,
                        source_kind VARCHAR(16) NOT NULL,
                        source_id VARCHAR(64) NOT NULL,
                        publish_job_id BIGINT UNSIGNED NULL,
                        state VARCHAR(16) NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        INDEX idx_publish_changes_job (publish_job_id),
                        INDEX idx_publish_changes_source (source_kind, source_id, id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
                self._ensure_ingest_progress_columns(cursor)
                self._ensure_message_citations_column(cursor)
                self._apply_schema_comments(cursor)
                self._ensure_index(cursor, "chats", "idx_chats_updated_at", "updated_at DESC")
                self._ensure_index(cursor, "chat_messages", "idx_chat_messages_chat_id_id", "chat_id, id")
                self._ensure_index(
                    cursor,
                    "chat_messages",
                    "idx_chat_messages_chat_id_created_at",
                    "chat_id, created_at",
                )
                self._ensure_index(cursor, "ingest_jobs", "idx_ingest_jobs_created_at", "created_at DESC")
                self._ensure_index(cursor, "publish_jobs", "idx_publish_jobs_schedule", "status, scheduled_at")

    @contextmanager
    def connect(self) -> Iterator[Any]:
        pymysql = self._import_pymysql()
        try:
            connection = pymysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                database=self._database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
        except pymysql.MySQLError as exc:
            raise StorageUnavailableError("Failed to connect to MySQL.") from exc

        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_chats(self) -> list[ChatResponse]:
        rows = self._fetch_all(
            """
            SELECT
                c.id,
                c.title,
                c.status,
                c.created_at,
                c.updated_at,
                c.last_message_at,
                (
                    SELECT m.content
                    FROM chat_messages AS m
                    WHERE m.chat_id = c.id
                    ORDER BY m.id DESC
                    LIMIT 1
                ) AS last_message_preview
            FROM chats AS c
            ORDER BY c.updated_at DESC
            """
        )
        return [self._chat_from_row(row) for row in rows]

    def create_chat(self, title: str) -> ChatResponse:
        now = self._utc_now()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chats (title, status, created_at, updated_at, last_message_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (title, "active", now, now, None),
                )
                chat_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    SELECT id, title, status, created_at, updated_at, last_message_at
                    FROM chats
                    WHERE id = %s
                    """,
                    (chat_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload created chat.")
        return self._chat_from_row(row)

    def get_chat(self, chat_id: int) -> ChatResponse | None:
        rows = self._fetch_all(
            """
            SELECT id, title, status, created_at, updated_at, last_message_at
            FROM chats
            WHERE id = %s
            """,
            (chat_id,),
        )
        if not rows:
            return None
        return self._chat_from_row(rows[0])

    def rename_chat(self, chat_id: int, title: str) -> ChatResponse:
        updated_at = self._utc_now()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE chats
                    SET title = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (title, updated_at, chat_id),
                )
                if cursor.rowcount == 0:
                    raise ChatNotFoundError(f"chat not found: {chat_id}")
                cursor.execute(
                    """
                    SELECT id, title, status, created_at, updated_at, last_message_at
                    FROM chats
                    WHERE id = %s
                    """,
                    (chat_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload renamed chat.")
        return self._chat_from_row(row)

    def list_messages(self, chat_id: int) -> list[ChatMessageResponse]:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                   created_at, synthesis_path, synthesized_at
            FROM chat_messages
            WHERE chat_id = %s
            ORDER BY id ASC
            """,
            (chat_id,),
        )
        return [self._message_from_row(row) for row in rows]

    def list_recent_messages(
        self,
        chat_id: int,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[ChatMessageResponse]:
        if before_message_id is None:
            query = """
                SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                       created_at, synthesis_path, synthesized_at
                FROM chat_messages
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
            """
            params: tuple[Any, ...] = (chat_id, limit)
        else:
            query = """
                SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                       created_at, synthesis_path, synthesized_at
                FROM chat_messages
                WHERE chat_id = %s AND id < %s
                ORDER BY id DESC
                LIMIT %s
            """
            params = (chat_id, before_message_id, limit)

        rows = self._fetch_all(query, params)
        messages = [self._message_from_row(row) for row in rows]
        messages.reverse()
        return messages

    def count_messages(self, chat_id: int) -> int:
        rows = self._fetch_all(
            """
            SELECT COUNT(*) AS message_count
            FROM chat_messages
            WHERE chat_id = %s
            """,
            (chat_id,),
        )
        return int(rows[0]["message_count"]) if rows else 0

    def create_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        sources: list[str] | None = None,
        relevant_pages: list[str] | None = None,
        citations: list[CitationResponse] | None = None,
    ) -> ChatMessageResponse:
        created_at = self._utc_now()
        serialized_sources = json.dumps(sources or [], ensure_ascii=False)
        serialized_relevant_pages = json.dumps(relevant_pages or [], ensure_ascii=False)
        serialized_citations = json.dumps(
            [citation.model_dump(mode="json") for citation in citations or []],
            ensure_ascii=False,
        )

        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_messages (
                        chat_id, role, content, sources, relevant_pages, citations, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chat_id,
                        role,
                        content,
                        serialized_sources,
                        serialized_relevant_pages,
                        serialized_citations,
                        created_at,
                    ),
                )
                message_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                           created_at, synthesis_path, synthesized_at
                    FROM chat_messages
                    WHERE id = %s
                    """,
                    (message_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload created message.")
        return self._message_from_row(row)

    def get_message(self, chat_id: int, message_id: int) -> ChatMessageResponse | None:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                   created_at, synthesis_path, synthesized_at
            FROM chat_messages
            WHERE chat_id = %s AND id = %s
            """,
            (chat_id, message_id),
        )
        if not rows:
            return None
        return self._message_from_row(rows[0])

    def get_previous_user_message(
        self,
        chat_id: int,
        before_message_id: int,
    ) -> ChatMessageResponse | None:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                   created_at, synthesis_path, synthesized_at
            FROM chat_messages
            WHERE chat_id = %s AND role = 'user' AND id < %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, before_message_id),
        )
        if not rows:
            return None
        return self._message_from_row(rows[0])

    def mark_message_synthesized(
        self,
        chat_id: int,
        message_id: int,
        synthesis_path: str,
        synthesized_at: datetime,
    ) -> ChatMessageResponse | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE chat_messages
                    SET synthesis_path = %s,
                        synthesized_at = %s
                    WHERE chat_id = %s
                      AND id = %s
                      AND role = 'assistant'
                      AND synthesis_path IS NULL
                    """,
                    (synthesis_path, synthesized_at, chat_id, message_id),
                )
                if cursor.rowcount == 0:
                    return None
                cursor.execute(
                    """
                    SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                           created_at, synthesis_path, synthesized_at
                    FROM chat_messages
                    WHERE chat_id = %s AND id = %s
                    """,
                    (chat_id, message_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload synthesized message.")
        return self._message_from_row(row)

    def create_ingest_job(
        self,
        *,
        status: str,
        original_filename: str,
        stored_filename: str,
        source_path: str,
        created_at: datetime,
    ) -> IngestJobResponse:
        empty_array = json.dumps([], ensure_ascii=False)
        empty_validation = json.dumps({"broken_links": [], "unindexed": []}, ensure_ascii=False)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ingest_jobs (
                        status, stage, progress_percent,
                        original_filename, stored_filename, source_path,
                        created_pages, updated_pages, contradictions, validation,
                        error, created_at, started_at, updated_at, finished_at
                    )
                    VALUES (%s, 'uploaded', 0, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL, %s, NULL)
                    """,
                    (
                        status,
                        original_filename,
                        stored_filename,
                        source_path,
                        empty_array,
                        empty_array,
                        empty_array,
                        empty_validation,
                        created_at,
                        created_at,
                    ),
                )
                job_id = int(cursor.lastrowid)
                cursor.execute("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload created ingest job.")
        return self._ingest_job_from_row(row)

    def get_ingest_job(self, job_id: int) -> IngestJobResponse | None:
        rows = self._fetch_all("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))
        if not rows:
            return None
        job = self._ingest_job_from_row(rows[0])
        return job.model_copy(update={"publication": self.get_publication(source_kind="ingest", source_id=str(job_id))})

    def list_ingest_jobs(self, limit: int) -> list[IngestJobResponse]:
        rows = self._fetch_all(
            """
            SELECT *
            FROM ingest_jobs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            job.model_copy(update={"publication": self.get_publication(source_kind="ingest", source_id=str(job.job_id))})
            for job in (self._ingest_job_from_row(row) for row in rows)
        ]

    def mark_ingest_job_running(self, job_id: int, started_at: datetime) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET status = 'running', started_at = %s, updated_at = %s, error = NULL
            WHERE id = %s
            """,
            (started_at, started_at, job_id),
        )

    def update_ingest_job_progress(
        self,
        *,
        job_id: int,
        stage: str,
        progress_percent: int,
        updated_at: datetime,
    ) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET stage = %s, progress_percent = %s, updated_at = %s
            WHERE id = %s
            """,
            (stage, progress_percent, updated_at, job_id),
        )

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: int,
        created_pages: list[str],
        updated_pages: list[str],
        contradictions: list[str],
        validation: IngestValidation,
        finished_at: datetime,
    ) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET status = 'succeeded',
                stage = 'completed',
                progress_percent = 100,
                created_pages = %s,
                updated_pages = %s,
                contradictions = %s,
                validation = %s,
                error = NULL,
                updated_at = %s,
                finished_at = %s
            WHERE id = %s
            """,
            (
                json.dumps(created_pages, ensure_ascii=False),
                json.dumps(updated_pages, ensure_ascii=False),
                json.dumps(contradictions, ensure_ascii=False),
                validation.model_dump_json(),
                finished_at,
                finished_at,
                job_id,
            ),
        )

    def mark_ingest_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET status = 'failed', error = %s, updated_at = %s, finished_at = %s
            WHERE id = %s
            """,
            (error, finished_at, finished_at, job_id),
        )

    def queue_publish_change(
        self,
        *,
        source_kind: str,
        source_id: str,
        scheduled_at: datetime,
        max_scheduled_at: datetime,
        now: datetime,
    ) -> PublishJobResponse:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM publish_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1 FOR UPDATE"
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO publish_jobs (
                            status, trigger_kind, scheduled_at, created_at, updated_at
                        ) VALUES ('queued', 'automatic', %s, %s, %s)
                        """,
                        (scheduled_at, now, now),
                    )
                    job_id = int(cursor.lastrowid)
                else:
                    job_id = int(row["id"])
                    max_delay = max_scheduled_at - now
                    deadline = row["created_at"] + max_delay
                    effective_schedule = min(scheduled_at, deadline)
                    cursor.execute(
                        "UPDATE publish_jobs SET scheduled_at = %s, updated_at = %s WHERE id = %s",
                        (effective_schedule, now, job_id),
                    )
                cursor.execute(
                    """
                    INSERT INTO publish_changes (
                        source_kind, source_id, publish_job_id, state, created_at, updated_at
                    ) VALUES (%s, %s, %s, 'pending', %s, %s)
                    """,
                    (source_kind, source_id, job_id, now, now),
                )
        return self.get_publish_job(job_id) or self._raise_missing_publish_job(job_id)

    def request_manual_publish(self, *, now: datetime) -> PublishJobResponse:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM publish_jobs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
                )
                running = cursor.fetchone()
                if running is not None:
                    job_id = int(running["id"])
                else:
                    cursor.execute(
                        "SELECT * FROM publish_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1 FOR UPDATE"
                    )
                    queued = cursor.fetchone()
                    if queued is not None:
                        job_id = int(queued["id"])
                        cursor.execute(
                            """
                            UPDATE publish_jobs
                            SET trigger_kind = 'manual', scheduled_at = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (now, now, job_id),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO publish_jobs (
                                status, trigger_kind, scheduled_at, created_at, updated_at
                            ) VALUES ('queued', 'manual', %s, %s, %s)
                            """,
                            (now, now, now),
                        )
                        job_id = int(cursor.lastrowid)
                        cursor.execute(
                            """
                            UPDATE publish_changes
                            SET publish_job_id = %s, state = 'pending', updated_at = %s
                            WHERE state = 'failed'
                            """,
                            (job_id, now),
                        )
        return self.get_publish_job(job_id) or self._raise_missing_publish_job(job_id)

    def claim_due_publish_job(self, *, now: datetime) -> PublishJobResponse | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM publish_jobs
                    WHERE status = 'queued' AND scheduled_at <= %s
                    ORDER BY scheduled_at ASC
                    LIMIT 1 FOR UPDATE
                    """,
                    (now,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                job_id = int(row["id"])
                cursor.execute(
                    "UPDATE publish_jobs SET status = 'running', started_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, job_id),
                )
                cursor.execute(
                    "UPDATE publish_changes SET state = 'running', updated_at = %s WHERE publish_job_id = %s AND state = 'pending'",
                    (now, job_id),
                )
        return self.get_publish_job(job_id)

    def mark_publish_job_succeeded(self, *, job_id: int, release_id: str, finished_at: datetime) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE publish_jobs
                    SET status = 'succeeded', release_id = %s, published_at = %s,
                        finished_at = %s, updated_at = %s, error = NULL
                    WHERE id = %s
                    """,
                    (release_id, finished_at, finished_at, finished_at, job_id),
                )
                cursor.execute(
                    "UPDATE publish_changes SET state = 'published', updated_at = %s WHERE publish_job_id = %s",
                    (finished_at, job_id),
                )

    def mark_publish_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE publish_jobs
                    SET status = 'failed', error = %s, finished_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (error, finished_at, finished_at, job_id),
                )
                cursor.execute(
                    "UPDATE publish_changes SET state = 'failed', updated_at = %s WHERE publish_job_id = %s",
                    (finished_at, job_id),
                )

    def recover_publish_jobs(self, *, now: datetime) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publish_jobs SET status = 'failed', error = 'publish worker restarted', finished_at = %s, updated_at = %s WHERE status = 'running'",
                    (now, now),
                )
                cursor.execute(
                    "UPDATE publish_changes SET state = 'pending', publish_job_id = NULL, updated_at = %s WHERE state = 'running'",
                    (now,),
                )
                cursor.execute("SELECT id FROM publish_changes WHERE state = 'pending' LIMIT 1")
                if cursor.fetchone() is None:
                    return
                cursor.execute("SELECT id FROM publish_jobs WHERE status = 'queued' LIMIT 1")
                if cursor.fetchone() is not None:
                    return
                cursor.execute(
                    "INSERT INTO publish_jobs (status, trigger_kind, scheduled_at, created_at, updated_at) VALUES ('queued', 'automatic', %s, %s, %s)",
                    (now, now, now),
                )
                job_id = int(cursor.lastrowid)
                cursor.execute(
                    "UPDATE publish_changes SET publish_job_id = %s, updated_at = %s WHERE state = 'pending' AND publish_job_id IS NULL",
                    (job_id, now),
                )

    def get_publish_job(self, job_id: int) -> PublishJobResponse | None:
        rows = self._fetch_all(self._publish_job_select("WHERE p.id = %s"), (job_id,))
        return self._publish_job_from_row(rows[0]) if rows else None

    def list_publish_jobs(self, limit: int) -> list[PublishJobResponse]:
        rows = self._fetch_all(self._publish_job_select("ORDER BY p.created_at DESC LIMIT %s"), (limit,))
        return [self._publish_job_from_row(row) for row in rows]

    def get_publish_status(self) -> PublishStatusResponse:
        pending_rows = self._fetch_all("SELECT COUNT(*) AS count FROM publish_changes WHERE state IN ('pending', 'running')")
        active_rows = self._fetch_all(self._publish_job_select("WHERE p.status IN ('queued', 'running') ORDER BY p.created_at ASC LIMIT 1"))
        successful_rows = self._fetch_all(self._publish_job_select("WHERE p.status = 'succeeded' ORDER BY p.published_at DESC LIMIT 1"))
        return PublishStatusResponse(
            pending_change_count=int(pending_rows[0]["count"]),
            active_job=self._publish_job_from_row(active_rows[0]) if active_rows else None,
            last_successful_job=self._publish_job_from_row(successful_rows[0]) if successful_rows else None,
        )

    def get_publication(self, *, source_kind: str, source_id: str) -> PublicationResponse | None:
        rows = self._fetch_all(
            """
            SELECT c.state, c.publish_job_id, p.published_at, p.error
            FROM publish_changes AS c
            LEFT JOIN publish_jobs AS p ON p.id = c.publish_job_id
            WHERE c.source_kind = %s AND c.source_id = %s
            ORDER BY c.id DESC LIMIT 1
            """,
            (source_kind, source_id),
        )
        if not rows:
            return None
        row = rows[0]
        return PublicationResponse(
            status=row["state"],
            job_id=int(row["publish_job_id"]) if row.get("publish_job_id") is not None else None,
            published_at=row.get("published_at"),
            error=row.get("error") if row["state"] == "failed" else None,
        )

    def update_chat_activity(
        self,
        chat_id: int,
        updated_at: datetime,
        last_message_at: datetime | None,
    ) -> ChatResponse:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE chats
                    SET updated_at = %s, last_message_at = %s
                    WHERE id = %s
                    """,
                    (updated_at, last_message_at, chat_id),
                )
                cursor.execute(
                    """
                    SELECT id, title, status, created_at, updated_at, last_message_at
                    FROM chats
                    WHERE id = %s
                    """,
                    (chat_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise ChatNotFoundError(f"chat not found: {chat_id}")
        return self._chat_from_row(row)

    def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("Storage query failed.") from exc
        return list(rows)

    def _execute_update(self, query: str, params: tuple[Any, ...]) -> None:
        try:
            with self.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("Storage update failed.") from exc

    @staticmethod
    def _ensure_message_citations_column(cursor: Any) -> None:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'chat_messages'
              AND COLUMN_NAME = 'citations'
            """
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            """
            ALTER TABLE chat_messages
            ADD COLUMN citations JSON NULL AFTER relevant_pages
            """
        )
        cursor.execute("UPDATE chat_messages SET citations = JSON_ARRAY() WHERE citations IS NULL")
        cursor.execute(
            """
            ALTER TABLE chat_messages
            MODIFY COLUMN citations JSON NOT NULL COMMENT '结构化Wiki引用列表（JSON）'
            """
        )

    @staticmethod
    def _ensure_ingest_progress_columns(cursor: Any) -> None:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'ingest_jobs'
              AND COLUMN_NAME IN ('stage', 'progress_percent', 'updated_at')
            """
        )
        existing = {row["COLUMN_NAME"] for row in cursor.fetchall()}
        if "stage" not in existing:
            cursor.execute(
                "ALTER TABLE ingest_jobs ADD COLUMN stage VARCHAR(32) NOT NULL DEFAULT 'uploaded' AFTER status"
            )
            cursor.execute(
                """
                UPDATE ingest_jobs
                SET stage = CASE
                    WHEN status = 'succeeded' THEN 'completed'
                    WHEN status = 'running' THEN 'extracting'
                    ELSE 'uploaded'
                END
                """
            )
        if "progress_percent" not in existing:
            cursor.execute(
                "ALTER TABLE ingest_jobs ADD COLUMN progress_percent TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER stage"
            )
            cursor.execute(
                """
                UPDATE ingest_jobs
                SET progress_percent = CASE
                    WHEN status = 'succeeded' THEN 100
                    WHEN status = 'running' THEN 35
                    ELSE 0
                END
                """
            )
        if "updated_at" not in existing:
            cursor.execute("ALTER TABLE ingest_jobs ADD COLUMN updated_at DATETIME NULL AFTER started_at")
            cursor.execute(
                """
                UPDATE ingest_jobs
                SET updated_at = COALESCE(finished_at, started_at, created_at)
                WHERE updated_at IS NULL
                """
            )
            cursor.execute("ALTER TABLE ingest_jobs MODIFY COLUMN updated_at DATETIME NOT NULL")

    @staticmethod
    def _apply_schema_comments(cursor: Any) -> None:
        # CREATE TABLE IF NOT EXISTS does not update comments on existing tables.
        expected_table_comments = {
            "chats": "聊天会话表",
            "chat_messages": "聊天消息表",
        }
        expected_column_comments = {
            "chats": {
                "id": "会话数字自增主键",
                "title": "会话标题",
                "status": "会话状态",
                "created_at": "创建时间（UTC）",
                "updated_at": "最后更新时间（UTC）",
                "last_message_at": "最后一条消息时间（UTC）",
            },
            "chat_messages": {
                "id": "消息自增主键",
                "chat_id": "所属会话数字ID",
                "role": "消息角色：user或assistant",
                "content": "消息正文",
                "sources": "回答引用来源列表（JSON）",
                "relevant_pages": "查询命中的Wiki页面列表（JSON）",
                "citations": "结构化Wiki引用列表（JSON）",
                "created_at": "创建时间（UTC）",
                "synthesis_path": "该助手消息保存成的Synthesis相对路径",
                "synthesized_at": "保存为Synthesis的时间（UTC）",
            },
        }
        cursor.execute(
            """
            SELECT TABLE_NAME, TABLE_COMMENT
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('chats', 'chat_messages')
            """
        )
        actual_table_comments = {
            row["TABLE_NAME"]: row["TABLE_COMMENT"] for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('chats', 'chat_messages')
            """
        )
        actual_column_comments: dict[str, dict[str, str]] = {
            "chats": {},
            "chat_messages": {},
        }
        actual_column_types: dict[str, dict[str, str]] = {
            "chats": {},
            "chat_messages": {},
        }
        for row in cursor.fetchall():
            actual_column_comments[row["TABLE_NAME"]][row["COLUMN_NAME"]] = row[
                "COLUMN_COMMENT"
            ]
            actual_column_types[row["TABLE_NAME"]][row["COLUMN_NAME"]] = row[
                "COLUMN_TYPE"
            ]

        if (
            actual_table_comments.get("chats") != expected_table_comments["chats"]
            or actual_column_comments["chats"] != expected_column_comments["chats"]
            or any(
                actual_column_types["chats"].get(column_name) != "datetime"
                for column_name in ("created_at", "updated_at", "last_message_at")
            )
            or actual_column_types["chats"].get("id") != "bigint unsigned"
        ):
            cursor.execute(
                """
                ALTER TABLE chats
                    MODIFY COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '会话数字自增主键',
                    MODIFY COLUMN title VARCHAR(200) NOT NULL COMMENT '会话标题',
                    MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '会话状态',
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '创建时间（UTC）',
                    MODIFY COLUMN updated_at DATETIME NOT NULL COMMENT '最后更新时间（UTC）',
                    MODIFY COLUMN last_message_at DATETIME NULL COMMENT '最后一条消息时间（UTC）',
                    COMMENT = '聊天会话表'
                """
            )
        if (
            actual_table_comments.get("chat_messages")
            != expected_table_comments["chat_messages"]
            or actual_column_comments["chat_messages"]
            != expected_column_comments["chat_messages"]
            or actual_column_types["chat_messages"].get("created_at") != "datetime"
            or actual_column_types["chat_messages"].get("synthesized_at") not in {None, "datetime"}
            or actual_column_types["chat_messages"].get("chat_id") != "bigint unsigned"
        ):
            if "synthesis_path" not in actual_column_types["chat_messages"]:
                cursor.execute(
                    """
                    ALTER TABLE chat_messages
                        ADD COLUMN synthesis_path VARCHAR(500) NULL
                            COMMENT '该助手消息保存成的Synthesis相对路径'
                    """
                )
            if "synthesized_at" not in actual_column_types["chat_messages"]:
                cursor.execute(
                    """
                    ALTER TABLE chat_messages
                        ADD COLUMN synthesized_at DATETIME NULL
                            COMMENT '保存为Synthesis的时间（UTC）'
                    """
                )
            cursor.execute(
                """
                ALTER TABLE chat_messages
                    MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT COMMENT '消息自增主键',
                    MODIFY COLUMN chat_id BIGINT UNSIGNED NOT NULL COMMENT '所属会话数字ID',
                    MODIFY COLUMN role VARCHAR(16) NOT NULL COMMENT '消息角色：user或assistant',
                    MODIFY COLUMN content TEXT NOT NULL COMMENT '消息正文',
                    MODIFY COLUMN sources JSON NOT NULL COMMENT '回答引用来源列表（JSON）',
                    MODIFY COLUMN relevant_pages JSON NOT NULL COMMENT '查询命中的Wiki页面列表（JSON）',
                    MODIFY COLUMN citations JSON NOT NULL COMMENT '结构化Wiki引用列表（JSON）',
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '创建时间（UTC）',
                    MODIFY COLUMN synthesis_path VARCHAR(500) NULL COMMENT '该助手消息保存成的Synthesis相对路径',
                    MODIFY COLUMN synthesized_at DATETIME NULL COMMENT '保存为Synthesis的时间（UTC）',
                    COMMENT = '聊天消息表'
                """
            )

    @staticmethod
    def _ensure_index(cursor: Any, table_name: str, index_name: str, columns_sql: str) -> None:
        cursor.execute("SHOW INDEX FROM " + table_name + " WHERE Key_name = %s", (index_name,))
        if cursor.fetchone() is None:
            cursor.execute(f"CREATE INDEX {index_name} ON {table_name}({columns_sql})")

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.utcnow().replace(microsecond=0)

    @staticmethod
    def _parse_json_field(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return []

    def _chat_from_row(self, row: dict[str, Any]) -> ChatResponse:
        return ChatResponse(
            id=int(row["id"]),
            title=str(row["title"]),
            status=str(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_message_at=row.get("last_message_at"),
            last_message_preview=row.get("last_message_preview"),
        )

    def _message_from_row(self, row: dict[str, Any]) -> ChatMessageResponse:
        return ChatMessageResponse(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            role=row["role"],
            content=str(row["content"]),
            sources=self._parse_json_field(row.get("sources")),
            relevant_pages=self._parse_json_field(row.get("relevant_pages")),
            citations=self._parse_citations_field(row.get("citations")),
            created_at=row["created_at"],
            synthesis_path=row.get("synthesis_path"),
            synthesized_at=row.get("synthesized_at"),
        )

    @staticmethod
    def _parse_citations_field(value: Any) -> list[CitationResponse]:
        parsed: Any = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
        if not isinstance(parsed, list):
            return []
        citations: list[CitationResponse] = []
        for item in parsed:
            try:
                citations.append(CitationResponse.model_validate(item))
            except (TypeError, ValueError):
                continue
        return citations

    def _ingest_job_from_row(self, row: dict[str, Any]) -> IngestJobResponse:
        return IngestJobResponse(
            job_id=int(row["id"]),
            status=row["status"],
            stage=row.get("stage", "uploaded"),
            progress_percent=int(row.get("progress_percent", 0)),
            original_filename=str(row["original_filename"]),
            source_path=str(row["source_path"]),
            created_pages=self._parse_json_field(row.get("created_pages")),
            updated_pages=self._parse_json_field(row.get("updated_pages")),
            contradictions=self._parse_json_field(row.get("contradictions")),
            validation=self._parse_ingest_validation(row.get("validation")),
            error=row.get("error"),
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            updated_at=row.get("updated_at") or row["created_at"],
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _publish_job_select(suffix: str) -> str:
        return """
            SELECT p.*, (
                SELECT COUNT(*) FROM publish_changes AS c WHERE c.publish_job_id = p.id
            ) AS change_count
            FROM publish_jobs AS p
        """ + suffix

    @staticmethod
    def _raise_missing_publish_job(job_id: int) -> PublishJobResponse:
        raise StorageError(f"Failed to reload publish job: {job_id}")

    @staticmethod
    def _publish_job_from_row(row: dict[str, Any]) -> PublishJobResponse:
        return PublishJobResponse(
            job_id=int(row["id"]),
            status=row["status"],
            trigger=row["trigger_kind"],
            change_count=int(row.get("change_count", 0)),
            scheduled_at=row["scheduled_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            published_at=row.get("published_at"),
            error=row.get("error"),
        )

    @staticmethod
    def _parse_ingest_validation(value: Any) -> IngestValidation:
        if isinstance(value, dict):
            return IngestValidation.model_validate(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return IngestValidation()
            if isinstance(parsed, dict):
                return IngestValidation.model_validate(parsed)
        return IngestValidation()

    @staticmethod
    def _import_pymysql() -> Any:
        try:
            import pymysql
        except ModuleNotFoundError as exc:
            raise StorageUnavailableError("PyMySQL is not installed.") from exc
        return pymysql


storage = MySQLStorage(
    host=settings.mysql_host,
    port=settings.mysql_port,
    user=settings.mysql_user,
    password=settings.mysql_password,
    database=settings.mysql_database,
)
