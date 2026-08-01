"""YAML task schema definition and parsing with defaults inheritance."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

MODEL_ALIASES = {
    "fast": "composer-2-fast",
    "think": "claude-opus-4-7-thinking-high",
    "codex": "gpt-5.3-codex",
    "codex-high": "gpt-5.3-codex-high",
    "max": "gpt-5.3-codex-xhigh",
    "opus": "claude-opus-4-7-high",
    "sonnet": "claude-4.6-sonnet-medium",
    "auto": "auto",
}


def resolve_model(name: str) -> str:
    """Resolve a model alias to its full name, or return as-is."""
    return MODEL_ALIASES.get(name, name) if name else ""


@dataclass
class StepConfig:
    prompt: str
    max_rounds: int = 5


@dataclass
class TaskConfig:
    name: str
    initial_prompt: str
    agent: str = "cursor"
    model: str = "claude-opus-4-7-high"
    effort: str = ""
    cwd: str = "."
    max_rounds: int = 10
    idle_timeout: int = 20
    monitor_mode: str = "fixed"
    monitor_interval: float = 1.0
    skills: list[str] = field(default_factory=list)
    steps: list[StepConfig] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    mode: str = "manual"
    completion_criteria: str = ""
    important_notes: str = ""


@dataclass
class ProjectConfig:
    project: str
    max_concurrent_agents: int = 3
    shared_constraints: dict = field(default_factory=dict)
    slack_webhook_url: str = ""
    tasks: list[TaskConfig] = field(default_factory=list)


def _merge_defaults(task_dict: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    merged.update({k: v for k, v in task_dict.items() if v is not None})
    return merged


def load_config(path: str | Path) -> ProjectConfig:
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    defaults = raw.get("defaults", {})
    project = ProjectConfig(
        project=raw.get("project", path.stem),
        max_concurrent_agents=raw.get("max_concurrent_agents", 3),
        shared_constraints=raw.get("shared_constraints", {}),
        slack_webhook_url=raw.get("slack_webhook_url", ""),
    )

    for t in raw.get("tasks", []):
        merged = _merge_defaults(t, defaults)

        steps = []
        for s in merged.pop("steps", []) or []:
            steps.append(StepConfig(
                prompt=s["prompt"],
                max_rounds=s.get("max_rounds", 5),
            ))

        depends_on = merged.pop("depends_on", []) or []
        name = merged.pop("name")
        initial_prompt = merged.pop("initial_prompt", "")

        task = TaskConfig(
            name=name,
            initial_prompt=initial_prompt,
            agent=merged.get("agent", "cursor"),
            model=resolve_model(merged.get("model", "")),
            effort=merged.get("effort", ""),
            cwd=merged.get("cwd", "."),
            max_rounds=merged.get("max_rounds", 10),
            idle_timeout=merged.get("idle_timeout", 20),
            monitor_mode=merged.get("monitor_mode", "fixed"),
            monitor_interval=merged.get("monitor_interval", 1.0),
            skills=merged.get("skills", []),
            steps=steps,
            depends_on=depends_on,
            mode=merged.get("mode", "manual"),
            completion_criteria=merged.get("completion_criteria", ""),
            important_notes=merged.get("important_notes", ""),
        )
        project.tasks.append(task)

    return project


def load_skills(skill_names: list[str], skills_dir: str | Path = "skills") -> str:
    skills_dir = Path(skills_dir)
    parts = []
    for name in skill_names:
        skill_file = skills_dir / f"{name}.md"
        if skill_file.exists():
            parts.append(skill_file.read_text().strip())
    return "\n\n---\n\n".join(parts)
