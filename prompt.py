"""Continuation prompt generation for auto-run mode."""

FIXED_PROMPT_BASE_ZH = (
    "请检查当前任务进度。如果任务已全部完成且验证通过，请回复'任务已完成'。"
    "否则请分析当前情况并继续执行。"
)

FIXED_PROMPT_BASE_EN = (
    "Check the current task progress. If the task is fully completed and verified, "
    "reply with 'TASK_COMPLETE'. Otherwise, analyze the current situation and continue."
)

COMPLETION_KEYWORDS = ["任务已完成", "TASK_COMPLETE", "task_complete"]


def get_continuation_prompt(
    mode: str = "fixed",
    lang: str = "zh",
    completion_criteria: str = "",
    important_notes: str = "",
) -> str:
    base = FIXED_PROMPT_BASE_ZH if lang == "zh" else FIXED_PROMPT_BASE_EN

    parts = []
    if completion_criteria:
        parts.append(f"[完成条件] {completion_criteria}")
    if important_notes:
        parts.append(f"[重要提醒] {important_notes}")
    parts.append(base)

    return "\n".join(parts)


def build_initial_context(
    initial_prompt: str,
    skills_text: str = "",
    completion_criteria: str = "",
    important_notes: str = "",
) -> str:
    """Build the full initial prompt with all context injected."""
    parts = []
    if skills_text:
        parts.append(f"[参考信息]\n{skills_text}")
    if completion_criteria:
        parts.append(f"[完成条件]\n{completion_criteria}")
    if important_notes:
        parts.append(f"[重要提醒]\n{important_notes}")
    parts.append(initial_prompt)
    return "\n\n".join(parts)
