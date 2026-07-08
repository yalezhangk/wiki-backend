from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import uuid4

from app.config import settings
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.ingest import IngestJobResponse, IngestValidation


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
                        id CHAR(36) PRIMARY KEY COMMENT '会话唯一标识（UUID）',
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
                        chat_id CHAR(36) NOT NULL COMMENT '所属会话ID',
                        role VARCHAR(16) NOT NULL COMMENT '消息角色：user或assistant',
                        content TEXT NOT NULL COMMENT '消息正文',
                        sources JSON NOT NULL COMMENT '回答引用来源列表（JSON）',
                        relevant_pages JSON NOT NULL COMMENT '查询命中的Wiki页面列表（JSON）',
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
                        id CHAR(36) PRIMARY KEY,
                        status VARCHAR(32) NOT NULL,
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
                        finished_at DATETIME NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
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
        chat_id = str(uuid4())
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chats (id, title, status, created_at, updated_at, last_message_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (chat_id, title, "active", now, now, None),
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
            raise StorageError("Failed to reload created chat.")
        return self._chat_from_row(row)

    def get_chat(self, chat_id: str) -> ChatResponse | None:
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

    def rename_chat(self, chat_id: str, title: str) -> ChatResponse:
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

    def list_messages(self, chat_id: str) -> list[ChatMessageResponse]:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
            FROM chat_messages
            WHERE chat_id = %s
            ORDER BY id ASC
            """,
            (chat_id,),
        )
        return [self._message_from_row(row) for row in rows]

    def list_recent_messages(
        self,
        chat_id: str,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[ChatMessageResponse]:
        if before_message_id is None:
            query = """
                SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
                FROM chat_messages
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
            """
            params: tuple[Any, ...] = (chat_id, limit)
        else:
            query = """
                SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
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

    def count_messages(self, chat_id: str) -> int:
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
        chat_id: str,
        role: str,
        content: str,
        sources: list[str] | None = None,
        relevant_pages: list[str] | None = None,
    ) -> ChatMessageResponse:
        created_at = self._utc_now()
        serialized_sources = json.dumps(sources or [], ensure_ascii=False)
        serialized_relevant_pages = json.dumps(relevant_pages or [], ensure_ascii=False)

        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_messages (chat_id, role, content, sources, relevant_pages, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chat_id,
                        role,
                        content,
                        serialized_sources,
                        serialized_relevant_pages,
                        created_at,
                    ),
                )
                message_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
                    FROM chat_messages
                    WHERE id = %s
                    """,
                    (message_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload created message.")
        return self._message_from_row(row)

    def get_message(self, chat_id: str, message_id: int) -> ChatMessageResponse | None:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
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
        chat_id: str,
        before_message_id: int,
    ) -> ChatMessageResponse | None:
        rows = self._fetch_all(
            """
            SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
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
        chat_id: str,
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
                    SELECT id, chat_id, role, content, sources, relevant_pages, created_at, synthesis_path, synthesized_at
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
        job_id: str,
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
                        id, status, original_filename, stored_filename, source_path,
                        created_pages, updated_pages, contradictions, validation,
                        error, created_at, started_at, finished_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL, NULL)
                    """,
                    (
                        job_id,
                        status,
                        original_filename,
                        stored_filename,
                        source_path,
                        empty_array,
                        empty_array,
                        empty_array,
                        empty_validation,
                        created_at,
                    ),
                )
                cursor.execute("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))
                row = cursor.fetchone()
        if row is None:
            raise StorageError("Failed to reload created ingest job.")
        return self._ingest_job_from_row(row)

    def get_ingest_job(self, job_id: str) -> IngestJobResponse | None:
        rows = self._fetch_all("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))
        if not rows:
            return None
        return self._ingest_job_from_row(rows[0])

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
        return [self._ingest_job_from_row(row) for row in rows]

    def mark_ingest_job_running(self, job_id: str, started_at: datetime) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET status = 'running', started_at = %s, error = NULL
            WHERE id = %s
            """,
            (started_at, job_id),
        )

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: str,
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
                created_pages = %s,
                updated_pages = %s,
                contradictions = %s,
                validation = %s,
                error = NULL,
                finished_at = %s
            WHERE id = %s
            """,
            (
                json.dumps(created_pages, ensure_ascii=False),
                json.dumps(updated_pages, ensure_ascii=False),
                json.dumps(contradictions, ensure_ascii=False),
                validation.model_dump_json(),
                finished_at,
                job_id,
            ),
        )

    def mark_ingest_job_failed(self, *, job_id: str, error: str, finished_at: datetime) -> None:
        self._execute_update(
            """
            UPDATE ingest_jobs
            SET status = 'failed', error = %s, finished_at = %s
            WHERE id = %s
            """,
            (error, finished_at, job_id),
        )

    def update_chat_activity(
        self,
        chat_id: str,
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
    def _apply_schema_comments(cursor: Any) -> None:
        # CREATE TABLE IF NOT EXISTS does not update comments on existing tables.
        expected_table_comments = {
            "chats": "聊天会话表",
            "chat_messages": "聊天消息表",
        }
        expected_column_comments = {
            "chats": {
                "id": "会话唯一标识（UUID）",
                "title": "会话标题",
                "status": "会话状态",
                "created_at": "创建时间（UTC）",
                "updated_at": "最后更新时间（UTC）",
                "last_message_at": "最后一条消息时间（UTC）",
            },
            "chat_messages": {
                "id": "消息自增主键",
                "chat_id": "所属会话ID",
                "role": "消息角色：user或assistant",
                "content": "消息正文",
                "sources": "回答引用来源列表（JSON）",
                "relevant_pages": "查询命中的Wiki页面列表（JSON）",
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
        ):
            cursor.execute(
                """
                ALTER TABLE chats
                    MODIFY COLUMN id CHAR(36) NOT NULL COMMENT '会话唯一标识（UUID）',
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
                    MODIFY COLUMN chat_id CHAR(36) NOT NULL COMMENT '所属会话ID',
                    MODIFY COLUMN role VARCHAR(16) NOT NULL COMMENT '消息角色：user或assistant',
                    MODIFY COLUMN content TEXT NOT NULL COMMENT '消息正文',
                    MODIFY COLUMN sources JSON NOT NULL COMMENT '回答引用来源列表（JSON）',
                    MODIFY COLUMN relevant_pages JSON NOT NULL COMMENT '查询命中的Wiki页面列表（JSON）',
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
            id=str(row["id"]),
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
            chat_id=str(row["chat_id"]),
            role=row["role"],
            content=str(row["content"]),
            sources=self._parse_json_field(row.get("sources")),
            relevant_pages=self._parse_json_field(row.get("relevant_pages")),
            created_at=row["created_at"],
            synthesis_path=row.get("synthesis_path"),
            synthesized_at=row.get("synthesized_at"),
        )

    def _ingest_job_from_row(self, row: dict[str, Any]) -> IngestJobResponse:
        return IngestJobResponse(
            job_id=str(row["id"]),
            status=row["status"],
            original_filename=str(row["original_filename"]),
            source_path=str(row["source_path"]),
            created_pages=self._parse_json_field(row.get("created_pages")),
            updated_pages=self._parse_json_field(row.get("updated_pages")),
            contradictions=self._parse_json_field(row.get("contradictions")),
            validation=self._parse_ingest_validation(row.get("validation")),
            error=row.get("error"),
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
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
