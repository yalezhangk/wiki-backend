from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from app.config import settings
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.ingest import IngestJobResponse, IngestTrigger, IngestValidation
from app.schemas.query import CitationResponse
from app.schemas.publish import PublicationResponse, PublishJobResponse, PublishStatusResponse
from app.time_utils import beijing_now
from app.schemas.maintenance import (
    MaintenanceJobResponse,
    MaintenanceResultState,
    MaintenanceTaskKind,
    MaintenanceTrigger,
)


class StorageError(RuntimeError):
    """Raised when a storage operation fails."""


class ChatNotFoundError(StorageError):
    """Raised when a chat cannot be found."""


class StorageUnavailableError(StorageError):
    """Raised when MySQL is unavailable or misconfigured."""


@dataclass(frozen=True)
class ScheduledIngestSource:
    source_id: int
    source_root: str
    relative_path: str
    state: str
    attempt_count: int
    ingest_job_id: int | None


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
                        created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                        updated_at DATETIME NOT NULL COMMENT '最后更新时间（北京时间）',
                        last_message_at DATETIME NULL COMMENT '最后一条消息时间（北京时间）'
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
                        model_profile_id VARCHAR(100) NULL COMMENT '生成回答时使用的模型档案ID',
                        model_profile_label VARCHAR(200) NULL COMMENT '生成回答时使用的模型显示名称快照',
                        created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                        synthesis_path VARCHAR(500) NULL COMMENT '该助手消息保存成的Synthesis相对路径',
                        synthesized_at DATETIME NULL COMMENT '保存为Synthesis的时间（北京时间）',
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
                        `trigger` VARCHAR(32) NOT NULL DEFAULT 'manual',
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
                    CREATE TABLE IF NOT EXISTS scheduled_ingest_sources (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
                        source_key CHAR(64) NOT NULL,
                        source_root VARCHAR(500) NOT NULL,
                        relative_path VARCHAR(1000) NOT NULL,
                        source_device BIGINT UNSIGNED NOT NULL,
                        source_inode BIGINT UNSIGNED NOT NULL,
                        state VARCHAR(32) NOT NULL,
                        first_seen_at DATETIME NOT NULL,
                        last_attempt_at DATETIME NOT NULL,
                        finished_at DATETIME NULL,
                        ingest_job_id BIGINT UNSIGNED NULL,
                        attempt_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
                        last_error VARCHAR(1000) NULL,
                        UNIQUE KEY uq_scheduled_ingest_source_key (source_key),
                        UNIQUE KEY uq_scheduled_ingest_file_identity (source_device, source_inode),
                        INDEX idx_scheduled_ingest_sources_state (state, last_attempt_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='DGX定时Markdown入库源文件清单'
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
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maintenance_jobs (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '维护任务数字自增主键',
                        task_kind VARCHAR(16) NOT NULL COMMENT '维护任务类型：health、graph或lint',
                        status VARCHAR(16) NOT NULL COMMENT '任务状态：queued、running、succeeded或failed',
                        result_state VARCHAR(16) NOT NULL DEFAULT 'unavailable' COMMENT '结果完整性：unavailable、partial或complete',
                        trigger_kind VARCHAR(16) NOT NULL COMMENT '触发方式：manual、automatic或workflow',
                        workflow_id CHAR(36) NULL COMMENT '所属质量工作流UUID',
                        depends_on_job_id BIGINT UNSIGNED NULL COMMENT '前置依赖维护任务ID',
                        stage VARCHAR(32) NOT NULL DEFAULT 'queued' COMMENT '当前执行阶段',
                        progress_percent TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '任务完成百分比（0至100）',
                        request_options JSON NOT NULL COMMENT '创建任务时的选项（JSON）',
                        result_summary JSON NOT NULL COMMENT '任务完成后的结构化结果摘要（JSON）',
                        error TEXT NULL COMMENT '安全截断后的失败错误摘要',
                        created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                        started_at DATETIME NULL COMMENT '开始执行时间（北京时间）',
                        updated_at DATETIME NOT NULL COMMENT '最后更新时间（北京时间）',
                        finished_at DATETIME NULL COMMENT '完成或失败时间（北京时间）'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='Wiki维护任务队列表'
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maintenance_page_state (
                        page_path VARCHAR(512) PRIMARY KEY COMMENT 'Wiki目录下的页面相对路径',
                        content_hash CHAR(64) NOT NULL COMMENT '当前页面内容的SHA-256哈希',
                        last_structural_checked_at DATETIME NULL COMMENT '最近完成结构检查时间（北京时间）',
                        last_semantic_checked_at DATETIME NULL COMMENT '最近完成语义检查时间（北京时间）',
                        last_semantic_content_hash CHAR(64) NULL COMMENT '最近语义检查对应的内容SHA-256哈希',
                        last_semantic_job_id BIGINT UNSIGNED NULL COMMENT '最近语义检查对应的维护任务ID'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='Wiki页面巡检状态表'
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maintenance_findings (
                        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '巡检发现数字自增主键',
                        job_id BIGINT UNSIGNED NOT NULL COMMENT '产生该发现的维护任务ID',
                        finding_type VARCHAR(32) NOT NULL COMMENT '发现类型，如broken_link、orphan或contradiction',
                        severity VARCHAR(16) NOT NULL COMMENT '严重级别：info、warning或error',
                        affected_pages JSON NOT NULL COMMENT '受影响页面相对路径列表（JSON）',
                        evidence JSON NOT NULL COMMENT '供人工核对的短证据列表（JSON）',
                        recommendation TEXT NOT NULL COMMENT '建议处理方式',
                        confidence DECIMAL(4,3) NULL COMMENT '语义发现的置信度（0至1）',
                        review_status VARCHAR(16) NOT NULL COMMENT '人工复核状态：needs_review、confirmed或dismissed',
                        created_at DATETIME NOT NULL COMMENT '发现写入时间（北京时间）'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='Wiki巡检发现表'
                    """
                )
                self._ensure_ingest_progress_columns(cursor)
                self._ensure_ingest_trigger_column(cursor)
                self._ensure_message_citations_column(cursor)
                self._ensure_message_model_profile_columns(cursor)
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
                self._ensure_index(cursor, "maintenance_jobs", "idx_maintenance_jobs_status_created", "status, created_at")
                self._ensure_index(cursor, "maintenance_jobs", "idx_maintenance_jobs_workflow_id", "workflow_id, id")
                self._ensure_index(cursor, "maintenance_jobs", "idx_maintenance_jobs_kind_finished", "task_kind, finished_at")
                self._ensure_index(cursor, "maintenance_jobs", "idx_maintenance_jobs_dependency", "depends_on_job_id")
                self._ensure_index(cursor, "maintenance_page_state", "idx_maintenance_page_state_semantic_at", "last_semantic_checked_at")
                self._ensure_index(cursor, "maintenance_page_state", "idx_maintenance_page_state_semantic_job", "last_semantic_job_id")
                self._ensure_index(cursor, "maintenance_findings", "idx_maintenance_findings_job", "job_id")

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
        now = self._beijing_now()
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
        updated_at = self._beijing_now()
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
                   model_profile_id, model_profile_label,
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
                       model_profile_id, model_profile_label,
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
                       model_profile_id, model_profile_label,
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
        model_profile_id: str | None = None,
        model_profile_label: str | None = None,
    ) -> ChatMessageResponse:
        created_at = self._beijing_now()
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
                        chat_id, role, content, sources, relevant_pages, citations,
                        model_profile_id, model_profile_label, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chat_id,
                        role,
                        content,
                        serialized_sources,
                        serialized_relevant_pages,
                        serialized_citations,
                        model_profile_id,
                        model_profile_label,
                        created_at,
                    ),
                )
                message_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    SELECT id, chat_id, role, content, sources, relevant_pages, citations,
                           model_profile_id, model_profile_label,
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
                   model_profile_id, model_profile_label,
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
                   model_profile_id, model_profile_label,
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
                           model_profile_id, model_profile_label,
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
        trigger: IngestTrigger = "manual",
        created_at: datetime,
    ) -> IngestJobResponse:
        empty_array = json.dumps([], ensure_ascii=False)
        empty_validation = json.dumps({"broken_links": [], "unindexed": []}, ensure_ascii=False)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ingest_jobs (
                        status, stage, progress_percent, `trigger`,
                        original_filename, stored_filename, source_path,
                        created_pages, updated_pages, contradictions, validation,
                        error, created_at, started_at, updated_at, finished_at
                    )
                    VALUES (%s, 'uploaded', 0, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL, %s, NULL)
                    """,
                    (
                        status,
                        trigger,
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

    def claim_scheduled_ingest_source(
        self,
        *,
        source_root: str,
        relative_path: str,
        source_device: int,
        source_inode: int,
        now: datetime,
    ) -> ScheduledIngestSource | None:
        source_key = self._scheduled_ingest_source_key(source_root, relative_path)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM scheduled_ingest_sources
                    WHERE source_key = %s
                       OR (source_device = %s AND source_inode = %s)
                    FOR UPDATE
                    """,
                    (source_key, source_device, source_inode),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return None
                cursor.execute(
                    """
                    INSERT INTO scheduled_ingest_sources (
                        source_key, source_root, relative_path, source_device, source_inode,
                        state, first_seen_at, last_attempt_at,
                        finished_at, ingest_job_id, attempt_count, last_error
                    ) VALUES (%s, %s, %s, %s, %s, 'processing', %s, %s, NULL, NULL, 0, NULL)
                    """,
                    (source_key, source_root, relative_path, source_device, source_inode, now, now),
                )
                source_id = int(cursor.lastrowid)
        return ScheduledIngestSource(
            source_id=source_id,
            source_root=source_root,
            relative_path=relative_path,
            state="processing",
            attempt_count=0,
            ingest_job_id=None,
        )

    def record_scheduled_ingest_attempt(
        self,
        *,
        source_id: int,
        ingest_job_id: int | None,
        attempted_at: datetime,
    ) -> None:
        self._execute_update(
            """
            UPDATE scheduled_ingest_sources
            SET attempt_count = attempt_count + 1,
                ingest_job_id = COALESCE(%s, ingest_job_id),
                last_attempt_at = %s,
                last_error = NULL
            WHERE id = %s AND state = 'processing'
            """,
            (ingest_job_id, attempted_at, source_id),
        )

    def set_scheduled_ingest_job(self, *, source_id: int, ingest_job_id: int) -> None:
        self._execute_update(
            """
            UPDATE scheduled_ingest_sources
            SET ingest_job_id = %s
            WHERE id = %s AND state = 'processing'
            """,
            (ingest_job_id, source_id),
        )

    def complete_scheduled_ingest_source(
        self,
        *,
        source_id: int,
        state: str,
        error: str | None,
        finished_at: datetime,
    ) -> None:
        if state not in {"succeeded", "failed"}:
            raise StorageError(f"Invalid scheduled ingest terminal state: {state}")
        self._execute_update(
            """
            UPDATE scheduled_ingest_sources
            SET state = %s, last_error = %s, finished_at = %s
            WHERE id = %s AND state = 'processing'
            """,
            (state, error, finished_at, source_id),
        )

    def recover_scheduled_ingest_sources(self, *, now: datetime) -> list[str]:
        recovery_errors: list[str] = []
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.id, s.relative_path, s.ingest_job_id,
                           j.status AS ingest_status, j.error AS ingest_error
                    FROM scheduled_ingest_sources AS s
                    LEFT JOIN ingest_jobs AS j ON j.id = s.ingest_job_id
                    WHERE s.state = 'processing'
                    FOR UPDATE
                    """
                )
                rows = cursor.fetchall()
                for row in rows:
                    source_id = int(row["id"])
                    if row["ingest_job_id"] is None:
                        # The request outcome is unknowable without a persisted job ID.  Mark it
                        # terminal rather than resubmitting and risking a duplicate Wiki write.
                        error = "同步进程在持久化 ingest job ID 前中断；为避免重复入库，不会自动重试"
                        cursor.execute(
                            """
                            UPDATE scheduled_ingest_sources
                            SET state = 'failed', finished_at = %s, last_error = %s
                            WHERE id = %s
                            """,
                            (now, error, source_id),
                        )
                        recovery_errors.append(f"{row['relative_path']}: {error}")
                    elif row["ingest_status"] == "succeeded":
                        cursor.execute(
                            """
                            UPDATE scheduled_ingest_sources
                            SET state = 'succeeded', finished_at = %s, last_error = NULL
                            WHERE id = %s
                            """,
                            (now, source_id),
                        )
                    elif row["ingest_status"] == "failed":
                        error = row["ingest_error"] or "关联的 ingest job 失败"
                        cursor.execute(
                            """
                            UPDATE scheduled_ingest_sources
                            SET state = 'failed', finished_at = %s, last_error = %s
                            WHERE id = %s
                            """,
                            (now, error, source_id),
                        )
                        recovery_errors.append(f"{row['relative_path']}: {error}")
                    elif row["ingest_status"] is None:
                        error = "关联的 ingest job 不存在"
                        cursor.execute(
                            """
                            UPDATE scheduled_ingest_sources
                            SET state = 'failed', finished_at = %s,
                                last_error = %s
                            WHERE id = %s
                            """,
                            (now, error, source_id),
                        )
                        recovery_errors.append(f"{row['relative_path']}: {error}")
                    else:
                        error = "关联的 ingest job 在前次同步结束后仍未进入终态"
                        cursor.execute(
                            """
                            UPDATE scheduled_ingest_sources
                            SET state = 'failed', finished_at = %s, last_error = %s
                            WHERE id = %s
                            """,
                            (now, error, source_id),
                        )
                        recovery_errors.append(f"{row['relative_path']}: {error}")
        return recovery_errors

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

    def create_maintenance_job(
        self,
        *,
        task_kind: MaintenanceTaskKind,
        trigger: MaintenanceTrigger,
        options: dict[str, Any],
        workflow_id: Any | None,
        depends_on_job_id: int | None,
        now: datetime,
    ) -> MaintenanceJobResponse:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO maintenance_jobs (
                        task_kind, status, result_state, trigger_kind, workflow_id,
                        depends_on_job_id, stage, progress_percent, request_options,
                        result_summary, created_at, updated_at
                    ) VALUES (%s, 'queued', 'unavailable', %s, %s, %s, 'queued', 0, %s, %s, %s, %s)
                    """,
                    (
                        task_kind,
                        trigger,
                        str(workflow_id) if workflow_id is not None else None,
                        depends_on_job_id,
                        json.dumps(options, ensure_ascii=False),
                        json.dumps({}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                job_id = int(cursor.lastrowid)
        return self.get_maintenance_job(job_id) or self._raise_missing_maintenance_job(job_id)

    def claim_due_maintenance_job(self, *, now: datetime) -> MaintenanceJobResponse | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE maintenance_jobs AS child
                    INNER JOIN maintenance_jobs AS dependency ON dependency.id = child.depends_on_job_id
                    SET child.status = 'failed', child.result_state = 'unavailable',
                        child.stage = 'dependency_failed', child.error = 'dependency job failed',
                        child.finished_at = %s, child.updated_at = %s
                    WHERE child.status = 'queued' AND dependency.status = 'failed'
                    """,
                    (now, now),
                )
                cursor.execute(
                    """
                    SELECT job.*
                    FROM maintenance_jobs AS job
                    LEFT JOIN maintenance_jobs AS dependency ON dependency.id = job.depends_on_job_id
                    WHERE job.status = 'queued'
                      AND (job.depends_on_job_id IS NULL OR dependency.status = 'succeeded')
                    ORDER BY job.created_at ASC, job.id ASC
                    LIMIT 1 FOR UPDATE
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                job_id = int(row["id"])
                cursor.execute(
                    """
                    UPDATE maintenance_jobs
                    SET status = 'running', stage = 'starting', progress_percent = 5,
                        started_at = %s, updated_at = %s, error = NULL
                    WHERE id = %s
                    """,
                    (now, now, job_id),
                )
        return self.get_maintenance_job(job_id)

    def mark_maintenance_job_succeeded(
        self,
        *,
        job_id: int,
        result_state: MaintenanceResultState,
        result_summary: dict[str, Any],
        finished_at: datetime,
    ) -> None:
        self._execute_update(
            """
            UPDATE maintenance_jobs
            SET status = 'succeeded', result_state = %s, stage = 'completed', progress_percent = 100,
                result_summary = %s, error = NULL, finished_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (result_state, json.dumps(result_summary, ensure_ascii=False), finished_at, finished_at, job_id),
        )

    def update_maintenance_job_progress(
        self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime
    ) -> None:
        self._execute_update(
            """
            UPDATE maintenance_jobs
            SET stage = %s, progress_percent = %s, updated_at = %s
            WHERE id = %s AND status = 'running'
            """,
            (stage, progress_percent, updated_at, job_id),
        )

    def mark_maintenance_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        self._execute_update(
            """
            UPDATE maintenance_jobs
            SET status = 'failed', result_state = 'unavailable', stage = 'failed',
                error = %s, finished_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (error, finished_at, finished_at, job_id),
        )

    def recover_maintenance_jobs(self, *, now: datetime) -> None:
        self._execute_update(
            """
            UPDATE maintenance_jobs
            SET status = 'failed', result_state = 'unavailable', stage = 'failed',
                error = 'maintenance worker restarted', finished_at = %s, updated_at = %s
            WHERE status = 'running'
            """,
            (now, now),
        )

    def upsert_maintenance_page_states(self, *, page_hashes: dict[str, str], checked_at: datetime) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                for page_path, content_hash in page_hashes.items():
                    cursor.execute(
                        """
                        INSERT INTO maintenance_page_state (page_path, content_hash, last_structural_checked_at)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE content_hash = VALUES(content_hash),
                            last_structural_checked_at = VALUES(last_structural_checked_at)
                        """,
                        (page_path, content_hash, checked_at),
                    )

    def get_maintenance_page_states(self) -> dict[str, dict[str, Any]]:
        rows = self._fetch_all(
            "SELECT page_path, content_hash, last_semantic_checked_at, last_semantic_content_hash FROM maintenance_page_state"
        )
        return {str(row["page_path"]): row for row in rows}

    def mark_maintenance_pages_semantically_checked(
        self, *, page_hashes: dict[str, str], job_id: int, checked_at: datetime
    ) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                for page_path, content_hash in page_hashes.items():
                    cursor.execute(
                        """
                        UPDATE maintenance_page_state
                        SET last_semantic_checked_at = %s, last_semantic_content_hash = %s,
                            last_semantic_job_id = %s
                        WHERE page_path = %s
                        """,
                        (checked_at, content_hash, job_id, page_path),
                    )

    def replace_maintenance_findings(self, *, job_id: int, findings: list[dict[str, Any]], created_at: datetime) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM maintenance_findings WHERE job_id = %s", (job_id,))
                for finding in findings:
                    cursor.execute(
                        """
                        INSERT INTO maintenance_findings (
                            job_id, finding_type, severity, affected_pages, evidence,
                            recommendation, confidence, review_status, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'needs_review', %s)
                        """,
                        (job_id, finding["finding_type"], finding["severity"], json.dumps(finding["affected_pages"], ensure_ascii=False), json.dumps(finding["evidence"], ensure_ascii=False), finding["recommendation"], finding.get("confidence"), created_at),
                    )

    def list_maintenance_findings(self, *, job_id: int) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            """
            SELECT id, finding_type, severity, affected_pages, evidence, recommendation,
                   review_status
            FROM maintenance_findings
            WHERE job_id = %s
            ORDER BY id ASC
            """,
            (job_id,),
        )
        return [
            {
                "finding_id": int(row["id"]),
                "finding_type": str(row["finding_type"]),
                "severity": str(row["severity"]),
                "affected_pages": self._parse_json_field(row.get("affected_pages")),
                "evidence": self._parse_json_list(row.get("evidence")),
                "recommendation": str(row["recommendation"] or ""),
                "review_status": str(row["review_status"]),
            }
            for row in rows
        ]

    def get_maintenance_job(self, job_id: int) -> MaintenanceJobResponse | None:
        rows = self._fetch_all(self._maintenance_job_select("WHERE m.id = %s"), (job_id,))
        return self._maintenance_job_from_row(rows[0]) if rows else None

    def list_maintenance_jobs(
        self,
        *,
        limit: int,
        task_kind: MaintenanceTaskKind | None,
        workflow_id: Any | None,
    ) -> list[MaintenanceJobResponse]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_kind is not None:
            clauses.append("m.task_kind = %s")
            params.append(task_kind)
        if workflow_id is not None:
            clauses.append("m.workflow_id = %s")
            params.append(str(workflow_id))
        suffix = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._fetch_all(
            self._maintenance_job_select(f"{suffix} ORDER BY m.created_at DESC, m.id DESC LIMIT %s"),
            tuple([*params, limit]),
        )
        return [self._maintenance_job_from_row(row) for row in rows]

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

    @staticmethod
    def _maintenance_job_select(suffix: str) -> str:
        return "SELECT m.* FROM maintenance_jobs AS m " + suffix

    @staticmethod
    def _raise_missing_maintenance_job(job_id: int) -> MaintenanceJobResponse:
        raise StorageError(f"Failed to reload maintenance job: {job_id}")

    @classmethod
    def _maintenance_job_from_row(cls, row: dict[str, Any]) -> MaintenanceJobResponse:
        return MaintenanceJobResponse(
            job_id=int(row["id"]),
            task_kind=row["task_kind"],
            status=row["status"],
            result_state=row["result_state"],
            trigger=row["trigger_kind"],
            workflow_id=row.get("workflow_id"),
            depends_on_job_id=(
                int(row["depends_on_job_id"])
                if row.get("depends_on_job_id") is not None
                else None
            ),
            stage=row["stage"],
            progress_percent=int(row["progress_percent"]),
            options=cls._parse_json_object(row.get("request_options")),
            result_summary=cls._parse_json_object(row.get("result_summary")),
            error=row.get("error"),
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            updated_at=row["updated_at"],
            finished_at=row.get("finished_at"),
        )

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
    def _ensure_message_model_profile_columns(cursor: Any) -> None:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'chat_messages'
              AND COLUMN_NAME IN ('model_profile_id', 'model_profile_label')
            """
        )
        existing_columns = {
            row["COLUMN_NAME"] if isinstance(row, dict) else row[0]
            for row in cursor.fetchall()
        }
        if "model_profile_id" not in existing_columns:
            cursor.execute(
                """
                ALTER TABLE chat_messages
                ADD COLUMN model_profile_id VARCHAR(100) NULL
                    COMMENT '生成回答时使用的模型档案ID'
                AFTER citations
                """
            )
        if "model_profile_label" not in existing_columns:
            cursor.execute(
                """
                ALTER TABLE chat_messages
                ADD COLUMN model_profile_label VARCHAR(200) NULL
                    COMMENT '生成回答时使用的模型显示名称快照'
                AFTER model_profile_id
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
    def _ensure_ingest_trigger_column(cursor: Any) -> None:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'ingest_jobs'
              AND COLUMN_NAME = 'trigger'
            """
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE ingest_jobs
                ADD COLUMN `trigger` VARCHAR(32) NOT NULL DEFAULT 'manual' AFTER progress_percent
                """
            )

    @staticmethod
    def _scheduled_ingest_source_key(source_root: str, relative_path: str) -> str:
        payload = f"{source_root}\x00{relative_path}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _apply_schema_comments(cursor: Any) -> None:
        # CREATE TABLE IF NOT EXISTS does not update comments on existing tables.
        expected_table_comments = {
            "chats": "聊天会话表",
            "chat_messages": "聊天消息表",
            "maintenance_jobs": "Wiki维护任务队列表",
            "maintenance_page_state": "Wiki页面巡检状态表",
            "maintenance_findings": "Wiki巡检发现表",
        }
        expected_column_comments = {
            "chats": {
                "id": "会话数字自增主键",
                "title": "会话标题",
                "status": "会话状态",
                "created_at": "创建时间（北京时间）",
                "updated_at": "最后更新时间（北京时间）",
                "last_message_at": "最后一条消息时间（北京时间）",
            },
            "chat_messages": {
                "id": "消息自增主键",
                "chat_id": "所属会话数字ID",
                "role": "消息角色：user或assistant",
                "content": "消息正文",
                "sources": "回答引用来源列表（JSON）",
                "relevant_pages": "查询命中的Wiki页面列表（JSON）",
                "citations": "结构化Wiki引用列表（JSON）",
                "model_profile_id": "生成回答时使用的模型档案ID",
                "model_profile_label": "生成回答时使用的模型显示名称快照",
                "created_at": "创建时间（北京时间）",
                "synthesis_path": "该助手消息保存成的Synthesis相对路径",
                "synthesized_at": "保存为Synthesis的时间（北京时间）",
            },
            "maintenance_jobs": {
                "id": "维护任务数字自增主键",
                "task_kind": "维护任务类型：health、graph或lint",
                "status": "任务状态：queued、running、succeeded或failed",
                "result_state": "结果完整性：unavailable、partial或complete",
                "trigger_kind": "触发方式：manual、automatic或workflow",
                "workflow_id": "所属质量工作流UUID",
                "depends_on_job_id": "前置依赖维护任务ID",
                "stage": "当前执行阶段",
                "progress_percent": "任务完成百分比（0至100）",
                "request_options": "创建任务时的选项（JSON）",
                "result_summary": "任务完成后的结构化结果摘要（JSON）",
                "error": "安全截断后的失败错误摘要",
                "created_at": "创建时间（北京时间）",
                "started_at": "开始执行时间（北京时间）",
                "updated_at": "最后更新时间（北京时间）",
                "finished_at": "完成或失败时间（北京时间）",
            },
            "maintenance_page_state": {
                "page_path": "Wiki目录下的页面相对路径",
                "content_hash": "当前页面内容的SHA-256哈希",
                "last_structural_checked_at": "最近完成结构检查时间（北京时间）",
                "last_semantic_checked_at": "最近完成语义检查时间（北京时间）",
                "last_semantic_content_hash": "最近语义检查对应的内容SHA-256哈希",
                "last_semantic_job_id": "最近语义检查对应的维护任务ID",
            },
            "maintenance_findings": {
                "id": "巡检发现数字自增主键",
                "job_id": "产生该发现的维护任务ID",
                "finding_type": "发现类型，如broken_link、orphan或contradiction",
                "severity": "严重级别：info、warning或error",
                "affected_pages": "受影响页面相对路径列表（JSON）",
                "evidence": "供人工核对的短证据列表（JSON）",
                "recommendation": "建议处理方式",
                "confidence": "语义发现的置信度（0至1）",
                "review_status": "人工复核状态：needs_review、confirmed或dismissed",
                "created_at": "发现写入时间（北京时间）",
            },
        }
        cursor.execute(
            """
            SELECT TABLE_NAME, TABLE_COMMENT
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN (
                  'chats', 'chat_messages', 'maintenance_jobs',
                  'maintenance_page_state', 'maintenance_findings'
              )
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
              AND TABLE_NAME IN (
                  'chats', 'chat_messages', 'maintenance_jobs',
                  'maintenance_page_state', 'maintenance_findings'
              )
            """
        )
        actual_column_comments: dict[str, dict[str, str]] = {
            "chats": {},
            "chat_messages": {},
            "maintenance_jobs": {},
            "maintenance_page_state": {},
            "maintenance_findings": {},
        }
        actual_column_types: dict[str, dict[str, str]] = {
            "chats": {},
            "chat_messages": {},
            "maintenance_jobs": {},
            "maintenance_page_state": {},
            "maintenance_findings": {},
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
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                    MODIFY COLUMN updated_at DATETIME NOT NULL COMMENT '最后更新时间（北京时间）',
                    MODIFY COLUMN last_message_at DATETIME NULL COMMENT '最后一条消息时间（北京时间）',
                    COMMENT = '聊天会话表'
                """
            )
        if (
            actual_table_comments.get("maintenance_jobs")
            != expected_table_comments["maintenance_jobs"]
            or actual_column_comments["maintenance_jobs"]
            != expected_column_comments["maintenance_jobs"]
        ):
            cursor.execute(
                """
                ALTER TABLE maintenance_jobs
                    MODIFY COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '维护任务数字自增主键',
                    MODIFY COLUMN task_kind VARCHAR(16) NOT NULL COMMENT '维护任务类型：health、graph或lint',
                    MODIFY COLUMN status VARCHAR(16) NOT NULL COMMENT '任务状态：queued、running、succeeded或failed',
                    MODIFY COLUMN result_state VARCHAR(16) NOT NULL DEFAULT 'unavailable' COMMENT '结果完整性：unavailable、partial或complete',
                    MODIFY COLUMN trigger_kind VARCHAR(16) NOT NULL COMMENT '触发方式：manual、automatic或workflow',
                    MODIFY COLUMN workflow_id CHAR(36) NULL COMMENT '所属质量工作流UUID',
                    MODIFY COLUMN depends_on_job_id BIGINT UNSIGNED NULL COMMENT '前置依赖维护任务ID',
                    MODIFY COLUMN stage VARCHAR(32) NOT NULL DEFAULT 'queued' COMMENT '当前执行阶段',
                    MODIFY COLUMN progress_percent TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '任务完成百分比（0至100）',
                    MODIFY COLUMN request_options JSON NOT NULL COMMENT '创建任务时的选项（JSON）',
                    MODIFY COLUMN result_summary JSON NOT NULL COMMENT '任务完成后的结构化结果摘要（JSON）',
                    MODIFY COLUMN error TEXT NULL COMMENT '安全截断后的失败错误摘要',
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                    MODIFY COLUMN started_at DATETIME NULL COMMENT '开始执行时间（北京时间）',
                    MODIFY COLUMN updated_at DATETIME NOT NULL COMMENT '最后更新时间（北京时间）',
                    MODIFY COLUMN finished_at DATETIME NULL COMMENT '完成或失败时间（北京时间）',
                    COMMENT = 'Wiki维护任务队列表'
                """
            )
        if (
            actual_table_comments.get("maintenance_page_state")
            != expected_table_comments["maintenance_page_state"]
            or actual_column_comments["maintenance_page_state"]
            != expected_column_comments["maintenance_page_state"]
        ):
            cursor.execute(
                """
                ALTER TABLE maintenance_page_state
                    MODIFY COLUMN page_path VARCHAR(512) NOT NULL COMMENT 'Wiki目录下的页面相对路径',
                    MODIFY COLUMN content_hash CHAR(64) NOT NULL COMMENT '当前页面内容的SHA-256哈希',
                    MODIFY COLUMN last_structural_checked_at DATETIME NULL COMMENT '最近完成结构检查时间（北京时间）',
                    MODIFY COLUMN last_semantic_checked_at DATETIME NULL COMMENT '最近完成语义检查时间（北京时间）',
                    MODIFY COLUMN last_semantic_content_hash CHAR(64) NULL COMMENT '最近语义检查对应的内容SHA-256哈希',
                    MODIFY COLUMN last_semantic_job_id BIGINT UNSIGNED NULL COMMENT '最近语义检查对应的维护任务ID',
                    COMMENT = 'Wiki页面巡检状态表'
                """
            )
        if (
            actual_table_comments.get("maintenance_findings")
            != expected_table_comments["maintenance_findings"]
            or actual_column_comments["maintenance_findings"]
            != expected_column_comments["maintenance_findings"]
        ):
            cursor.execute(
                """
                ALTER TABLE maintenance_findings
                    MODIFY COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '巡检发现数字自增主键',
                    MODIFY COLUMN job_id BIGINT UNSIGNED NOT NULL COMMENT '产生该发现的维护任务ID',
                    MODIFY COLUMN finding_type VARCHAR(32) NOT NULL COMMENT '发现类型，如broken_link、orphan或contradiction',
                    MODIFY COLUMN severity VARCHAR(16) NOT NULL COMMENT '严重级别：info、warning或error',
                    MODIFY COLUMN affected_pages JSON NOT NULL COMMENT '受影响页面相对路径列表（JSON）',
                    MODIFY COLUMN evidence JSON NOT NULL COMMENT '供人工核对的短证据列表（JSON）',
                    MODIFY COLUMN recommendation TEXT NOT NULL COMMENT '建议处理方式',
                    MODIFY COLUMN confidence DECIMAL(4,3) NULL COMMENT '语义发现的置信度（0至1）',
                    MODIFY COLUMN review_status VARCHAR(16) NOT NULL COMMENT '人工复核状态：needs_review、confirmed或dismissed',
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '发现写入时间（北京时间）',
                    COMMENT = 'Wiki巡检发现表'
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
                            COMMENT '保存为Synthesis的时间（北京时间）'
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
                    MODIFY COLUMN model_profile_id VARCHAR(100) NULL COMMENT '生成回答时使用的模型档案ID',
                    MODIFY COLUMN model_profile_label VARCHAR(200) NULL COMMENT '生成回答时使用的模型显示名称快照',
                    MODIFY COLUMN created_at DATETIME NOT NULL COMMENT '创建时间（北京时间）',
                    MODIFY COLUMN synthesis_path VARCHAR(500) NULL COMMENT '该助手消息保存成的Synthesis相对路径',
                    MODIFY COLUMN synthesized_at DATETIME NULL COMMENT '保存为Synthesis的时间（北京时间）',
                    COMMENT = '聊天消息表'
                """
            )

    @staticmethod
    def _ensure_index(cursor: Any, table_name: str, index_name: str, columns_sql: str) -> None:
        cursor.execute("SHOW INDEX FROM " + table_name + " WHERE Key_name = %s", (index_name,))
        if cursor.fetchone() is None:
            cursor.execute(f"CREATE INDEX {index_name} ON {table_name}({columns_sql})")

    @staticmethod
    def _beijing_now() -> datetime:
        return beijing_now()

    @staticmethod
    def _parse_json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

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

    @staticmethod
    def _parse_json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
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
            model_profile_id=row.get("model_profile_id"),
            model_profile_label=row.get("model_profile_label"),
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
            trigger=row.get("trigger", "manual"),
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
