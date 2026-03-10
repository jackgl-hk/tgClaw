from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from datetime import datetime


@dataclass
class Task:
    task_id: str
    chat_id: int
    text: str
    workdir: str
    status: str
    requires_approval: bool
    pid: int | None = None
    output: str | None = None
    error: str | None = None
    phase: str | None = None
    plan: list[str] | None = None
    attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "text": self.text,
            "workdir": self.workdir,
            "status": self.status,
            "requires_approval": self.requires_approval,
            "pid": self.pid,
            "output": self.output,
            "error": self.error,
            "phase": self.phase,
            "plan": self.plan,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Task":
        return Task(
            task_id=data["task_id"],
            chat_id=int(data["chat_id"]),
            text=data["text"],
            workdir=data["workdir"],
            status=data["status"],
            requires_approval=bool(data.get("requires_approval", False)),
            pid=data.get("pid"),
            output=data.get("output"),
            error=data.get("error"),
            phase=data.get("phase"),
            plan=data.get("plan"),
            attempts=int(data.get("attempts", 0)),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


class TaskStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"tasks": {}, "chat_state": {}}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {"tasks": {}, "chat_state": {}}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    def create_task(
        self,
        task_id: str,
        chat_id: int,
        text: str,
        workdir: str,
        requires_approval: bool,
    ) -> Task:
        task = Task(
            task_id=task_id,
            chat_id=chat_id,
            text=text,
            workdir=workdir,
            status="pending",
            requires_approval=requires_approval,
            phase="queued",
            plan=[],
            attempts=0,
            created_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self.data["tasks"][task_id] = task.to_dict()
            self._save()
        return task

    def update_task(self, task: Task) -> None:
        task.updated_at = _now()
        with self._lock:
            self.data["tasks"][task.task_id] = task.to_dict()
            self._save()

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            data = self.data.get("tasks", {}).get(task_id)
            if not data:
                return None
            return Task.from_dict(data)

    def list_tasks_for_chat(self, chat_id: int) -> list[Task]:
        tasks = []
        with self._lock:
            items = list(self.data.get("tasks", {}).values())
        for item in items:
            if int(item.get("chat_id")) == chat_id:
                tasks.append(Task.from_dict(item))
        tasks.sort(key=lambda t: t.created_at or "")
        return tasks

    def list_all_tasks(self) -> list[Task]:
        with self._lock:
            items = list(self.data.get("tasks", {}).values())
        return [Task.from_dict(item) for item in items]

    def set_chat_workdir(self, chat_id: int, workdir: str) -> None:
        with self._lock:
            state = self.data.setdefault("chat_state", {}).get(str(chat_id), {})
            state["workdir"] = workdir
            self.data["chat_state"][str(chat_id)] = state
            self._save()

    def get_chat_workdir(self, chat_id: int) -> str | None:
        with self._lock:
            state = self.data.get("chat_state", {}).get(str(chat_id))
            if not state:
                return None
            return state.get("workdir")

    def set_chat_provider(self, chat_id: int, provider: str) -> None:
        with self._lock:
            state = self.data.setdefault("chat_state", {}).get(str(chat_id), {})
            state["provider"] = provider
            self.data["chat_state"][str(chat_id)] = state
            self._save()

    def get_chat_provider(self, chat_id: int) -> str | None:
        with self._lock:
            state = self.data.get("chat_state", {}).get(str(chat_id))
            if not state:
                return None
            return state.get("provider")

    def clear_chat(self, chat_id: int) -> None:
        with self._lock:
            self.data["tasks"] = {
                k: v for k, v in self.data.get("tasks", {}).items() if int(v.get("chat_id")) != chat_id
            }
            self.data.get("chat_state", {}).pop(str(chat_id), None)
            self._save()
