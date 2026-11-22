"""Run and measure isolated Codex sessions on the same router-payment materials."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from strategist import STAGES, changed_checkpoints, read_json


ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "cases" / "router-payment" / "inputs"
CASE = "router-live"
ANSWER = ("Ответь кратко на русском. Под заголовками «Решение», «Данные» и «Следующий шаг» "
          "назови владельца действия, кратко обоснуй вывод и сошлись на входные файлы. "
          "Учебные данные явно пометь. Утверждения об эффекте не подменяй оценкой исходной частоты.")
DIRECTIVE = "Используй MCP-сервер strategist. Используй его именованные инструменты, без shell и других источников."
PROMPTS = {
    "prepare": (
        DIRECTIVE + " Вызови start. Прочитай brief.md и cohorts.csv, вызови calculate для cohorts. "
        "Прочитай нужные сохранённые этапы, если они уже есть. Сохрани через save_stage только оставшиеся "
        "Markdown-файлы в указанном порядке. В research используй [[B-02]], "
        "в стратегии свяжи порог и неизвестную своевременность с [[B-01]] и [[A-01]], "
        "сравни текущий сценарий, напоминание, облегчённую альтернативу и закрытие. "
        "Приведи точные входы и результат расчёта с [[B-02]]. "
        "В конце запроси подтверждение действий, которые требуют утверждения. " + ANSWER
    ),
    "review": (
        DIRECTIVE + " Ты в новой сессии, прежнего чата нет. Вызови status, затем прочитай сохранённую strategy.md. "
        "Прочитай новый update.csv и вызови calculate для update. "
        "Сам выбери затронутые ID, вызови register_update, используй найденные строки. "
        "Актуализируй решение и сохрани review-1.md через save_stage. Включи [[S-01]], исходное решение, "
        "точную дробь, долю и разрыв к порогу. Укажи следующее действие и условия возврата к решению. " + ANSWER
    ),
}
ARTIFACTS = {"prepare": tuple(f"{stage}.md" for stage in STAGES), "review": ("review-1.md",)}


def read(path):
    return path.read_text(encoding="utf-8-sig")


def summarize(log):
    events = [json.loads(line) for line in read(log).splitlines() if line.strip()]
    session = next(event["thread_id"] for event in events if event["type"] == "thread.started")
    usage = next(event["usage"] for event in reversed(events) if event["type"] == "turn.completed")
    messages = [event["item"] for event in events if event["type"] == "item.completed"]
    text = [item["text"] for item in messages if item["type"] == "agent_message"][-1]
    tools = [{"name": item["tool"], "status": item.get("status"), "error": bool(item.get("error"))}
             for item in messages if item["type"] == "mcp_tool_call" and item.get("server") == "strategist"]
    other_tools = [item["type"] for item in messages
                   if item["type"] in {"command_execution", "web_search"}
                   or (item["type"] == "mcp_tool_call" and item.get("server") != "strategist")]
    return {"session": session, "usage": usage, "answer": text, "tools": tools,
            "other_tools": other_tools}


def baseline_prompt(phase, work):
    lead = ("Обычный чат без инструментария и без сохранённого состояния. "
            "Используй только переданные ниже материалы, никаких инструментов. ")
    inputs = "\n\n".join(f"{name}\n{read(INPUTS / name)}" for name in ("brief.md", "cohorts.csv"))
    if phase == "baseline-prepare":
        return lead + "Оцени готовность к пилоту и варианты, затем предложи продуктовый шаг. " + ANSWER + "\n\n" + inputs
    prior = json.loads(read(work / "baseline-prepare-summary.json"))["answer"]
    return (lead + "В новой сессии пользователь вручную перенёс предыдущую рекомендацию. "
            "Сопоставь её с новым наблюдением и пересмотри решение. " + ANSWER + "\n\n" + inputs
            + "\n\nПредыдущий ответ\n" + prior + "\n\nupdate.csv\n" + read(INPUTS / "update.csv"))


def write_activity(work):
    lines = ["# Журнал учебного кейса", "", "Модель и инструменты запускались через Codex CLI и локальный MCP.", ""]
    for phase in PROMPTS:
        path = work / f"{phase}-summary.json"
        if not path.is_file():
            continue
        run = json.loads(read(path))
        usage = run["usage"]
        successful = [item["name"] for item in run["tools"] if item["status"] == "completed"]
        failed = [item["name"] for item in run["tools"] if item["status"] != "completed"]
        lines.extend([f"## {phase}", "", f"- Длительность {run['seconds']:.2f} с.",
                      f"- Входных токенов {usage['input_tokens']}, выходных {usage['output_tokens']}.",
                      f"- Завершённые вызовы — {', '.join(successful) or 'нет'}.",
                      f"- Ошибки вызовов — {', '.join(failed) or 'нет'}.", ""])
        if run.get("previous_attempt"):
            lines.extend([f"Журнал предыдущей попытки сохранён в `{run['previous_attempt']}`.", ""])
    lines.append("Денежные списания и время человека отдельно не измерялись.")
    (work / "cases" / CASE / "activity.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def saved_artifacts(work):
    case = work / "cases" / CASE
    path = case / "state.json"
    if not path.is_file():
        return ()
    state = read_json(path)
    changed = changed_checkpoints(case, state)
    if changed:
        raise ValueError(f"Сначала восстановите контрольные точки: {', '.join(changed)}.")
    return tuple(entry["artifact"] for entry in state["checkpoints"])


def archive_attempt(work, phase):
    files = [work / f"{phase}{suffix}" for suffix in (".jsonl", "-diagnostics.txt", "-summary.json")]
    existing = [path for path in files if path.is_file()]
    if not existing:
        return None
    attempts = work / "attempts"
    attempts.mkdir(exist_ok=True)
    archived = Path(tempfile.mkdtemp(prefix=f"{phase}-", dir=attempts))
    for path in existing:
        path.replace(archived / path.name)
    return archived.relative_to(work).as_posix()


def codex_run(phase, work, model=None):
    arguments = ["codex"]
    if phase not in PROMPTS:
        arguments += ["--ask-for-approval", "never"]
    arguments += ["exec", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never"]
    if model:
        arguments += ["--model", model]
    if phase in PROMPTS:
        saved = saved_artifacts(work)
        if phase == "review" and not set(ARTIFACTS["prepare"]).issubset(saved):
            raise ValueError("Сначала завершите этапы первой сессии.")
        pending = tuple(path for path in ARTIFACTS[phase] if path not in saved)
        if not pending:
            print(f"Этапы {phase} уже сохранены. Результаты находятся в {work / 'cases' / CASE}.")
            return
        previous_attempt = archive_attempt(work, phase)
        arguments += ["--approve-for-me"]
        configuration = {
            "command": sys.executable, "cwd": str(ROOT),
            "args": ["-u", str(ROOT / "live_mcp.py"), "--work", str(work / "cases"),
                     "--case", CASE, "--phase", phase],
            "startup_timeout_sec": 20, "tool_timeout_sec": 30,
        }
        for key, value in configuration.items():
            arguments += ["-c", f"mcp_servers.strategist.{key}={json.dumps(value, ensure_ascii=False)}"]
        prompt = (PROMPTS[phase] + f" В этом сеансе осталось сохранить: {', '.join(pending)}. "
                  + (f"На диске уже сохранены: {', '.join(saved)}. " if saved else ""))
    else:
        arguments += ["--sandbox", "read-only"]
        prompt = baseline_prompt(phase, work)
    start = time.perf_counter()
    log = work / f"{phase}.jsonl"
    with log.open("wb") as stdout, (work / f"{phase}-diagnostics.txt").open("wb") as stderr:
        subprocess.run([*arguments, "-"], cwd=ROOT, input=prompt.encode("utf-8"), stdout=stdout,
                       stderr=stderr, timeout=360, check=True)
    result = summarize(log)
    result["seconds"] = round(time.perf_counter() - start, 2)
    if phase in PROMPTS:
        result["resumed_from"] = list(saved)
        if previous_attempt:
            result["previous_attempt"] = previous_attempt
    (work / f"{phase}-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if phase in PROMPTS:
        statuses = [tool["status"] for tool in result["tools"] if tool["name"] == "save_stage"]
        if statuses.count("completed") != len(pending) or not set(pending).issubset(saved_artifacts(work)):
            raise ValueError(f"Ожидались контрольные точки {', '.join(pending)}. Подробности в {log}.")
        write_activity(work)
    elif result["tools"] or result["other_tools"]:
        raise ValueError(f"Контрольная сессия использовала инструмент. Подробности в {log}.")
    print(json.dumps({key: value for key, value in result.items() if key != "answer"}, ensure_ascii=False))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=(*PROMPTS, "baseline-prepare", "baseline-review"))
    parser.add_argument("--work", type=Path, default=ROOT / "dist" / "live-router")
    parser.add_argument("--model")
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    codex_run(args.phase, work, args.model)


if __name__ == "__main__":
    main()
