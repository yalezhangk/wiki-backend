from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings

LOGGER = logging.getLogger(__name__)
BACKUP_SUFFIX = "_uuid_backup"
SHADOW_SUFFIX = "_numeric"


class MigrationError(RuntimeError):
    """UUID 主键迁移的前置检查或验证失败。"""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def _connect(config: DatabaseConfig) -> Any:
    try:
        import pymysql
    except ImportError as exc:
        raise MigrationError("PyMySQL is required to run the migration.") from exc
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _table_exists(cursor: Any, name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (name,),
    )
    return cursor.fetchone() is not None


def _column_type(cursor: Any, table_name: str, column_name: str) -> str | None:
    cursor.execute(
        """
        SELECT COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table_name, column_name),
    )
    row = cursor.fetchone()
    return str(row["COLUMN_TYPE"]).lower() if row is not None else None


def _count(cursor: Any, query: str, params: tuple[Any, ...] = ()) -> int:
    cursor.execute(query, params)
    row = cursor.fetchone()
    if row is None:
        raise MigrationError("Migration count query returned no result.")
    return int(row["count"])


def _require_legacy_schema(cursor: Any) -> None:
    expected = {
        ("chats", "id"): "char(36)",
        ("chat_messages", "chat_id"): "char(36)",
        ("ingest_jobs", "id"): "char(36)",
        ("publish_jobs", "id"): "char(36)",
        ("publish_changes", "publish_job_id"): "char(36)",
    }
    for (table_name, column_name), expected_type in expected.items():
        actual_type = _column_type(cursor, table_name, column_name)
        if actual_type != expected_type:
            raise MigrationError(
                f"Expected legacy {table_name}.{column_name} to be {expected_type}; "
                f"found {actual_type!r}."
            )

    for table_name in ("chats", "chat_messages", "ingest_jobs", "publish_jobs", "publish_changes"):
        if _table_exists(cursor, table_name + BACKUP_SUFFIX):
            raise MigrationError(f"Backup table already exists: {table_name + BACKUP_SUFFIX}")
        if _table_exists(cursor, table_name + SHADOW_SUFFIX):
            raise MigrationError(f"Shadow table already exists: {table_name + SHADOW_SUFFIX}")

    orphan_changes = _count(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM publish_changes AS changes
        LEFT JOIN publish_jobs AS jobs ON jobs.id = changes.publish_job_id
        WHERE changes.publish_job_id IS NOT NULL AND jobs.id IS NULL
        """,
    )
    if orphan_changes:
        raise MigrationError(f"Found {orphan_changes} publish_changes rows without a publish job.")
    orphan_ingest_sources = _count(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM publish_changes AS changes
        LEFT JOIN ingest_jobs AS jobs ON jobs.id = changes.source_id
        WHERE changes.source_kind = 'ingest' AND jobs.id IS NULL
        """,
    )
    if orphan_ingest_sources:
        raise MigrationError(f"Found {orphan_ingest_sources} ingest publish changes without an ingest job.")


def _create_shadow_tables(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE chats_numeric (
            id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '会话数字自增主键',
            legacy_id CHAR(36) NOT NULL,
            title VARCHAR(200) NOT NULL COMMENT '会话标题',
            status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '会话状态',
            created_at DATETIME NOT NULL COMMENT '创建时间（UTC）',
            updated_at DATETIME NOT NULL COMMENT '最后更新时间（UTC）',
            last_message_at DATETIME NULL COMMENT '最后一条消息时间（UTC）',
            UNIQUE KEY uq_chats_numeric_legacy_id (legacy_id),
            INDEX idx_chats_updated_at (updated_at DESC)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        COMMENT='聊天会话表'
        """
    )
    cursor.execute(
        """
        CREATE TABLE chat_messages_numeric (
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
            INDEX idx_chat_messages_chat_id_id (chat_id, id),
            INDEX idx_chat_messages_chat_id_created_at (chat_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        COMMENT='聊天消息表'
        """
    )
    cursor.execute(
        """
        CREATE TABLE ingest_jobs_numeric LIKE ingest_jobs
        """
    )
    cursor.execute(
        """
        ALTER TABLE ingest_jobs_numeric
            ADD COLUMN legacy_id CHAR(36) NOT NULL AFTER id,
            ADD UNIQUE KEY uq_ingest_jobs_numeric_legacy_id (legacy_id),
            MODIFY COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
        """
    )
    cursor.execute("CREATE TABLE publish_jobs_numeric LIKE publish_jobs")
    cursor.execute(
        """
        ALTER TABLE publish_jobs_numeric
            ADD COLUMN legacy_id CHAR(36) NOT NULL AFTER id,
            ADD UNIQUE KEY uq_publish_jobs_numeric_legacy_id (legacy_id),
            MODIFY COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
        """
    )
    cursor.execute("CREATE TABLE publish_changes_numeric LIKE publish_changes")
    cursor.execute(
        "ALTER TABLE publish_changes_numeric MODIFY COLUMN publish_job_id BIGINT UNSIGNED NULL"
    )


def _copy_data(cursor: Any) -> None:
    cursor.execute(
        """
        INSERT INTO chats_numeric (legacy_id, title, status, created_at, updated_at, last_message_at)
        SELECT id, title, status, created_at, updated_at, last_message_at
        FROM chats
        ORDER BY created_at ASC, id ASC
        """
    )
    cursor.execute(
        """
        INSERT INTO chat_messages_numeric (
            id, chat_id, role, content, sources, relevant_pages, citations,
            created_at, synthesis_path, synthesized_at
        )
        SELECT messages.id, chats.id, messages.role, messages.content, messages.sources,
               messages.relevant_pages, messages.citations, messages.created_at,
               messages.synthesis_path, messages.synthesized_at
        FROM chat_messages AS messages
        JOIN chats_numeric AS chats ON chats.legacy_id = messages.chat_id
        ORDER BY messages.id ASC
        """
    )
    cursor.execute(
        """
        INSERT INTO ingest_jobs_numeric (
            legacy_id, status, stage, progress_percent, original_filename, stored_filename,
            source_path, created_pages, updated_pages, contradictions, validation, error,
            created_at, started_at, updated_at, finished_at
        )
        SELECT id, status, stage, progress_percent, original_filename, stored_filename,
               source_path, created_pages, updated_pages, contradictions, validation, error,
               created_at, started_at, updated_at, finished_at
        FROM ingest_jobs
        ORDER BY created_at ASC, id ASC
        """
    )
    cursor.execute(
        """
        INSERT INTO publish_jobs_numeric (
            legacy_id, status, trigger_kind, scheduled_at, created_at, updated_at,
            started_at, finished_at, published_at, release_id, error
        )
        SELECT id, status, trigger_kind, scheduled_at, created_at, updated_at,
               started_at, finished_at, published_at, release_id, error
        FROM publish_jobs
        ORDER BY created_at ASC, id ASC
        """
    )
    cursor.execute(
        """
        INSERT INTO publish_changes_numeric (
            id, source_kind, source_id, publish_job_id, state, created_at, updated_at
        )
        SELECT changes.id,
               changes.source_kind,
               CASE
                   WHEN changes.source_kind = 'ingest' THEN CAST(ingest_jobs.id AS CHAR)
                   ELSE changes.source_id
               END,
               publish_jobs.id,
               changes.state, changes.created_at, changes.updated_at
        FROM publish_changes AS changes
        LEFT JOIN publish_jobs_numeric AS publish_jobs
            ON publish_jobs.legacy_id = changes.publish_job_id
        LEFT JOIN ingest_jobs_numeric AS ingest_jobs
            ON ingest_jobs.legacy_id = changes.source_id
        ORDER BY changes.id ASC
        """
    )


def _validate_shadow_data(cursor: Any) -> None:
    for legacy_name, shadow_name in (
        ("chats", "chats_numeric"),
        ("chat_messages", "chat_messages_numeric"),
        ("ingest_jobs", "ingest_jobs_numeric"),
        ("publish_jobs", "publish_jobs_numeric"),
        ("publish_changes", "publish_changes_numeric"),
    ):
        legacy_count = _count(cursor, f"SELECT COUNT(*) AS count FROM {legacy_name}")
        shadow_count = _count(cursor, f"SELECT COUNT(*) AS count FROM {shadow_name}")
        if legacy_count != shadow_count:
            raise MigrationError(
                f"Row count mismatch for {legacy_name}: {legacy_count} != {shadow_count}."
            )
    orphan_messages = _count(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM chat_messages_numeric AS messages
        LEFT JOIN chats_numeric AS chats ON chats.id = messages.chat_id
        WHERE chats.id IS NULL
        """,
    )
    orphan_publish_changes = _count(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM publish_changes_numeric AS changes
        LEFT JOIN publish_jobs_numeric AS jobs ON jobs.id = changes.publish_job_id
        WHERE changes.publish_job_id IS NOT NULL AND jobs.id IS NULL
        """,
    )
    if orphan_messages or orphan_publish_changes:
        raise MigrationError(
            "Shadow table relation validation failed: "
            f"orphan_messages={orphan_messages}, orphan_publish_changes={orphan_publish_changes}."
        )


def _swap_tables(cursor: Any) -> None:
    for table_name, index_name in (
        ("chats_numeric", "uq_chats_numeric_legacy_id"),
        ("ingest_jobs_numeric", "uq_ingest_jobs_numeric_legacy_id"),
        ("publish_jobs_numeric", "uq_publish_jobs_numeric_legacy_id"),
    ):
        cursor.execute(f"ALTER TABLE {table_name} DROP INDEX {index_name}, DROP COLUMN legacy_id")

    cursor.execute("ALTER TABLE chat_messages DROP FOREIGN KEY fk_chat_messages_chat_id")
    cursor.execute(
        """
        RENAME TABLE
            chats TO chats_uuid_backup,
            chat_messages TO chat_messages_uuid_backup,
            ingest_jobs TO ingest_jobs_uuid_backup,
            publish_jobs TO publish_jobs_uuid_backup,
            publish_changes TO publish_changes_uuid_backup,
            chats_numeric TO chats,
            chat_messages_numeric TO chat_messages,
            ingest_jobs_numeric TO ingest_jobs,
            publish_jobs_numeric TO publish_jobs,
            publish_changes_numeric TO publish_changes
        """
    )
    cursor.execute(
        """
        ALTER TABLE chat_messages
            ADD CONSTRAINT fk_chat_messages_chat_id_numeric
            FOREIGN KEY (chat_id) REFERENCES chats(id)
            ON DELETE CASCADE
        """
    )


def migrate(config: DatabaseConfig) -> None:
    connection = _connect(config)
    try:
        with connection.cursor() as cursor:
            _require_legacy_schema(cursor)
            _create_shadow_tables(cursor)
            _copy_data(cursor)
            _validate_shadow_data(cursor)
            connection.commit()
            _swap_tables(cursor)
            connection.commit()
            LOGGER.info("UUID primary key migration completed; backup tables were retained.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finalize(config: DatabaseConfig) -> None:
    connection = _connect(config)
    try:
        with connection.cursor() as cursor:
            for table_name in ("chats", "chat_messages", "ingest_jobs", "publish_jobs", "publish_changes"):
                backup_name = table_name + BACKUP_SUFFIX
                if not _table_exists(cursor, backup_name):
                    raise MigrationError(f"Backup table does not exist: {backup_name}")
            cursor.execute(
                "DROP TABLE publish_changes_uuid_backup, publish_jobs_uuid_backup, "
                "ingest_jobs_uuid_backup, chat_messages_uuid_backup, chats_uuid_backup"
            )
        connection.commit()
        LOGGER.info("UUID backup tables were removed.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate wiki-backend UUID primary keys to integers.")
    parser.add_argument("command", choices=("migrate", "finalize"))
    parser.add_argument("--confirm", action="store_true", help="Required acknowledgement for schema changes.")
    parser.add_argument("--database", default=settings.mysql_database, help="Target database name.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args()
    if not args.confirm:
        raise SystemExit("Refusing to change schema without --confirm.")
    config = DatabaseConfig(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=args.database,
    )
    if args.command == "migrate":
        migrate(config)
    else:
        finalize(config)


if __name__ == "__main__":
    main()
