from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.schemas.chat import ChatMessageResponse
from app.schemas.synthesis import SynthesisResponse
from app.services.chat_service import ChatService
from app.storage.mysql import StorageError

LOGGER = logging.getLogger(__name__)


class SynthesisServiceError(RuntimeError):
    """保存 Synthesis 失败。"""


class InvalidSynthesisMessageError(SynthesisServiceError):
    """指定 Message 不是可保存的 assistant answer。"""


class SynthesisAlreadyExistsError(SynthesisServiceError):
    """该 assistant answer 已保存。"""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class SynthesisQuestionNotFoundError(SynthesisServiceError):
    """找不到该 assistant answer 对应的 user question。"""


class SynthesisWriteError(SynthesisServiceError):
    """写入 Wiki 文件失败。"""


class SynthesisService:
    def __init__(self, chat_service: ChatService, wiki_repo_path: Path) -> None:
        self._chat_service = chat_service
        self._wiki_root = wiki_repo_path / "wiki"
        self._syntheses_dir = self._wiki_root / "syntheses"
        self._index_path = self._wiki_root / "index.md"
        self._log_path = self._wiki_root / "log.md"
        self._lock = threading.Lock()

    def save_chat_answer(
        self,
        *,
        chat_id: str,
        assistant_message_id: int,
        title: str | None,
    ) -> SynthesisResponse:
        assistant_message = self._chat_service.get_message(chat_id, assistant_message_id)
        if assistant_message.role != "assistant":
            raise InvalidSynthesisMessageError("message is not an assistant answer")
        if assistant_message.synthesis_path:
            raise SynthesisAlreadyExistsError(
                "message has already been saved as synthesis",
                assistant_message.synthesis_path,
            )

        question_message = self._chat_service.get_previous_user_message(chat_id, assistant_message_id)
        if question_message is None:
            raise SynthesisQuestionNotFoundError("previous user question not found")

        synthesis_title = self._normalize_title(title or question_message.content)
        created_at = datetime.utcnow().replace(microsecond=0)

        with self._lock:
            relative_path = self._allocate_relative_path(synthesis_title, created_at)
            synthesis_path = self._wiki_root / relative_path
            markdown = self._render_markdown(
                title=synthesis_title,
                assistant_message=assistant_message,
                question_message=question_message,
                created_at=created_at,
            )
            self._write_with_compensation(
                synthesis_path=synthesis_path,
                relative_path=relative_path,
                markdown=markdown,
                title=synthesis_title,
                chat_id=chat_id,
                assistant_message_id=assistant_message_id,
                created_at=created_at,
            )

        return SynthesisResponse(
            chat_id=chat_id,
            assistant_message_id=assistant_message_id,
            question_message_id=question_message.id,
            title=synthesis_title,
            path=relative_path.as_posix(),
            created_at=created_at,
        )

    def _write_with_compensation(
        self,
        *,
        synthesis_path: Path,
        relative_path: Path,
        markdown: str,
        title: str,
        chat_id: str,
        assistant_message_id: int,
        created_at: datetime,
    ) -> None:
        old_index = self._read_text_if_exists(self._index_path)
        old_log = self._read_text_if_exists(self._log_path)
        synthesis_created = False
        try:
            self._syntheses_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(synthesis_path, markdown)
            synthesis_created = True
            self._update_index(title=title, relative_path=relative_path)
            self._update_log(
                title=title,
                relative_path=relative_path,
                chat_id=chat_id,
                assistant_message_id=assistant_message_id,
                created_at=created_at,
            )
            updated_message = self._chat_service.mark_message_synthesized(
                chat_id=chat_id,
                message_id=assistant_message_id,
                synthesis_path=relative_path.as_posix(),
                synthesized_at=created_at,
            )
            if updated_message is None:
                raise SynthesisAlreadyExistsError("message has already been saved as synthesis")
        except SynthesisAlreadyExistsError:
            self._rollback_files(synthesis_path, synthesis_created, old_index, old_log)
            raise
        except StorageError:
            self._rollback_files(synthesis_path, synthesis_created, old_index, old_log)
            raise
        except Exception as exc:
            self._rollback_files(synthesis_path, synthesis_created, old_index, old_log)
            raise SynthesisWriteError("failed to save synthesis") from exc

    def _rollback_files(
        self,
        synthesis_path: Path,
        synthesis_created: bool,
        old_index: str | None,
        old_log: str | None,
    ) -> None:
        try:
            if synthesis_created and synthesis_path.exists():
                synthesis_path.unlink()
            self._restore_text(self._index_path, old_index)
            self._restore_text(self._log_path, old_log)
        except OSError as exc:
            LOGGER.exception("Failed to roll back synthesis files: %s", exc)

    def _update_index(self, *, title: str, relative_path: Path) -> None:
        current = self._read_text_if_exists(self._index_path) or ""
        link_path = relative_path.as_posix()
        if f"]({link_path})" in current:
            return

        entry = f"- [{title}]({link_path}) - synthesis\n"
        heading = "## Syntheses"
        if heading not in current:
            suffix = "" if current.endswith("\n") or not current else "\n"
            self._atomic_write(self._index_path, f"{current}{suffix}\n{heading}\n\n{entry}")
            return

        lines = current.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() == heading:
                insert_at = index + 1
                while insert_at < len(lines) and lines[insert_at].strip() == "":
                    insert_at += 1
                lines.insert(insert_at, entry)
                if insert_at == index + 1:
                    lines.insert(insert_at, "\n")
                self._atomic_write(self._index_path, "".join(lines))
                return

    def _update_log(
        self,
        *,
        title: str,
        relative_path: Path,
        chat_id: str,
        assistant_message_id: int,
        created_at: datetime,
    ) -> None:
        current = self._read_text_if_exists(self._log_path) or ""
        date_text = created_at.date().isoformat()
        entry = (
            f"## [{date_text}] synthesis | {title}\n\n"
            f"Saved chat answer {assistant_message_id} from chat {chat_id} "
            f"to {relative_path.as_posix()}.\n\n"
        )
        self._atomic_write(self._log_path, entry + current)

    def _allocate_relative_path(self, title: str, created_at: datetime) -> Path:
        slug = self._slugify(title)
        if not slug:
            slug = f"synthesis-{created_at.strftime('%Y%m%d-%H%M%S')}"
        candidate = Path("syntheses") / f"{slug}.md"
        suffix = 2
        while (self._wiki_root / candidate).exists():
            candidate = Path("syntheses") / f"{slug}-{suffix}.md"
            suffix += 1
        return candidate

    @staticmethod
    def _normalize_title(value: str) -> str:
        normalized = " ".join(value.split())
        return normalized[:80].rstrip() or "Untitled synthesis"

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = " ".join(value.split()).lower()
        normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f，。！？、；：\"'（）【】《》]", "", normalized)
        normalized = re.sub(r"\s+", "-", normalized)
        normalized = normalized.strip(".- ")
        return normalized[:80].rstrip(".- ")

    def _render_markdown(
        self,
        *,
        title: str,
        assistant_message: ChatMessageResponse,
        question_message: ChatMessageResponse,
        created_at: datetime,
    ) -> str:
        frontmatter = [
            "---",
            f"title: {self._yaml_string(title)}",
            "type: synthesis",
            "tags: []",
            "sources:",
            *self._yaml_list(assistant_message.sources),
            "relevant_pages:",
            *self._yaml_list(assistant_message.relevant_pages),
            f"source_chat_id: {self._yaml_string(assistant_message.chat_id)}",
            f"source_question_message_id: {question_message.id}",
            f"source_assistant_message_id: {assistant_message.id}",
            f"last_updated: {created_at.date().isoformat()}",
            "---",
            "",
        ]
        return "\n".join(frontmatter) + assistant_message.content.rstrip() + "\n"

    @staticmethod
    def _yaml_string(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _yaml_list(self, values: list[str]) -> list[str]:
        if not values:
            return []
        return [f"  - {self._yaml_string(value)}" for value in values]

    @staticmethod
    def _read_text_if_exists(path: Path) -> str | None:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _restore_text(self, path: Path, content: str | None) -> None:
        if content is None:
            if path.exists():
                path.unlink()
            return
        self._atomic_write(path, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
