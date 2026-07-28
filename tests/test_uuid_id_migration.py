from __future__ import annotations

import os
import unittest
from datetime import datetime

from app.config import settings
from tools.migrate_uuid_primary_keys import DatabaseConfig, _connect, migrate


MIGRATION_DATABASE = os.getenv("WIKI_BACKEND_MYSQL_MIGRATION_TEST_DATABASE")


@unittest.skipUnless(
    os.getenv("WIKI_BACKEND_RUN_MYSQL_MIGRATION_INTEGRATION") == "1" and MIGRATION_DATABASE,
    "UUID primary key migration integration test is disabled",
)
class UUIDPrimaryKeyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert MIGRATION_DATABASE is not None
        if MIGRATION_DATABASE == settings.mysql_database:
            self.fail("WIKI_BACKEND_MYSQL_MIGRATION_TEST_DATABASE must not be the application database")
        self.config = DatabaseConfig(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=MIGRATION_DATABASE,
        )
        self._create_database()
        self._create_legacy_schema()
        self._seed_legacy_data()

    def tearDown(self) -> None:
        self._drop_database()

    def test_migrate_preserves_data_and_rewrites_references(self) -> None:
        migrate(self.config)

        with _connect(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM chats WHERE title = '迁移会话'")
                chat_id = int(cursor.fetchone()["id"])
                cursor.execute("SELECT chat_id FROM chat_messages WHERE id = 7")
                self.assertEqual(int(cursor.fetchone()["chat_id"]), chat_id)

                cursor.execute("SELECT id FROM ingest_jobs WHERE original_filename = 'legacy.md'")
                ingest_id = int(cursor.fetchone()["id"])
                cursor.execute("SELECT id FROM publish_jobs WHERE release_id = 'legacy-release'")
                publish_id = int(cursor.fetchone()["id"])

                cursor.execute(
                    "SELECT source_kind, source_id, publish_job_id FROM publish_changes ORDER BY id"
                )
                changes = cursor.fetchall()
                self.assertEqual(changes[0]["source_id"], str(ingest_id))
                self.assertEqual(int(changes[0]["publish_job_id"]), publish_id)
                self.assertEqual(changes[1]["source_id"], "7")
                self.assertEqual(int(changes[1]["publish_job_id"]), publish_id)

                cursor.execute(
                    "INSERT INTO chats (title, status, created_at, updated_at, last_message_at) "
                    "VALUES ('迁移后会话', 'active', %s, %s, NULL)",
                    (datetime(2026, 7, 28), datetime(2026, 7, 28)),
                )
                self.assertGreater(int(cursor.lastrowid), chat_id)

                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND (TABLE_NAME, COLUMN_NAME) IN (
                          ('chats', 'id'), ('chat_messages', 'chat_id'),
                          ('ingest_jobs', 'id'), ('publish_jobs', 'id'),
                          ('publish_changes', 'publish_job_id')
                      )
                    """
                )
                types = {
                    (row["TABLE_NAME"], row["COLUMN_NAME"]): row["COLUMN_TYPE"]
                    for row in cursor.fetchall()
                }
        self.assertEqual(
            types,
            {
                ("chats", "id"): "bigint unsigned",
                ("chat_messages", "chat_id"): "bigint unsigned",
                ("ingest_jobs", "id"): "bigint unsigned",
                ("publish_jobs", "id"): "bigint unsigned",
                ("publish_changes", "publish_job_id"): "bigint unsigned",
            },
        )

    def _create_database(self) -> None:
        connection = _connect(
            DatabaseConfig(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database="mysql",
            )
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{self.config.database}`")
                cursor.execute(
                    f"CREATE DATABASE `{self.config.database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            connection.commit()
        finally:
            connection.close()

    def _drop_database(self) -> None:
        connection = _connect(
            DatabaseConfig(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database="mysql",
            )
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{self.config.database}`")
            connection.commit()
        finally:
            connection.close()

    def _create_legacy_schema(self) -> None:
        statements = (
            """
            CREATE TABLE chats (
                id CHAR(36) PRIMARY KEY, title VARCHAR(200) NOT NULL, status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, last_message_at DATETIME NULL
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE chat_messages (
                id BIGINT PRIMARY KEY AUTO_INCREMENT, chat_id CHAR(36) NOT NULL, role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL, sources JSON NOT NULL, relevant_pages JSON NOT NULL, citations JSON NOT NULL,
                created_at DATETIME NOT NULL, synthesis_path VARCHAR(500) NULL, synthesized_at DATETIME NULL,
                CONSTRAINT fk_chat_messages_chat_id
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE ingest_jobs (
                id CHAR(36) PRIMARY KEY, status VARCHAR(32) NOT NULL, stage VARCHAR(32) NOT NULL,
                progress_percent TINYINT UNSIGNED NOT NULL, original_filename VARCHAR(255) NOT NULL,
                stored_filename VARCHAR(255) NOT NULL, source_path VARCHAR(500) NOT NULL,
                created_pages JSON NOT NULL, updated_pages JSON NOT NULL, contradictions JSON NOT NULL,
                validation JSON NOT NULL, error TEXT NULL, created_at DATETIME NOT NULL,
                started_at DATETIME NULL, updated_at DATETIME NOT NULL, finished_at DATETIME NULL
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE publish_jobs (
                id CHAR(36) PRIMARY KEY, status VARCHAR(16) NOT NULL, trigger_kind VARCHAR(16) NOT NULL,
                scheduled_at DATETIME NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                started_at DATETIME NULL, finished_at DATETIME NULL, published_at DATETIME NULL,
                release_id VARCHAR(64) NULL, error TEXT NULL
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE publish_changes (
                id BIGINT PRIMARY KEY AUTO_INCREMENT, source_kind VARCHAR(16) NOT NULL,
                source_id VARCHAR(64) NOT NULL, publish_job_id CHAR(36) NULL, state VARCHAR(16) NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                INDEX idx_publish_changes_job (publish_job_id),
                INDEX idx_publish_changes_source (source_kind, source_id, id)
            ) ENGINE=InnoDB
            """,
        )
        with _connect(self.config) as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

    def _seed_legacy_data(self) -> None:
        chat_uuid = "11111111-1111-1111-1111-111111111111"
        ingest_uuid = "22222222-2222-2222-2222-222222222222"
        publish_uuid = "33333333-3333-3333-3333-333333333333"
        now = datetime(2026, 7, 28)
        with _connect(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO chats VALUES (%s, '迁移会话', 'active', %s, %s, NULL)",
                    (chat_uuid, now, now),
                )
                cursor.execute(
                    "INSERT INTO chat_messages VALUES (7, %s, 'assistant', '答案', JSON_ARRAY(), JSON_ARRAY(), JSON_ARRAY(), %s, NULL, NULL)",
                    (chat_uuid, now),
                )
                cursor.execute(
                    """
                    INSERT INTO ingest_jobs VALUES (
                        %s, 'succeeded', 'completed', 100, 'legacy.md', 'legacy.md',
                        'raw/uploads/legacy.md', JSON_ARRAY(), JSON_ARRAY(), JSON_ARRAY(),
                        JSON_OBJECT('broken_links', JSON_ARRAY(), 'unindexed', JSON_ARRAY()), NULL,
                        %s, NULL, %s, %s
                    )
                    """,
                    (ingest_uuid, now, now, now),
                )
                cursor.execute(
                    "INSERT INTO publish_jobs VALUES (%s, 'succeeded', 'automatic', %s, %s, %s, NULL, %s, %s, 'legacy-release', NULL)",
                    (publish_uuid, now, now, now, now, now),
                )
                cursor.execute(
                    "INSERT INTO publish_changes VALUES (1, 'ingest', %s, %s, 'published', %s, %s)",
                    (ingest_uuid, publish_uuid, now, now),
                )
                cursor.execute(
                    "INSERT INTO publish_changes VALUES (2, 'synthesis', '7', %s, 'published', %s, %s)",
                    (publish_uuid, now, now),
                )


if __name__ == "__main__":
    unittest.main()
