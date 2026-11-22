"""Score a small, paired comparison of two Codex workflows on the router case."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PHASES = ("prepare", "review", "baseline-prepare", "baseline-review")
RUBRIC = (
    ("prepare", "Исходная доля отключений 96 из 640 = 15%",
     (r"\b96\b", r"\b640\b", r"\b15\s*%"), r"cohorts\.csv|\[\[B-02\]\]"),
    ("prepare", "Условие готовности 90% событий за 24 часа",
     (r"\b90\s*%", r"\b24\s*(?:ч|час)"), r"brief\.md|\[\[B-01\]\]"),
    ("review", "Новый замер 280 из 400 = 70%",
     (r"\b280\b", r"\b400\b", r"\b70\s*%"), r"update\.csv|\[\[S-01\]\]"),
    ("review", "Разрыв 20 п.п. при пороге 90%",
     (r"\b20\s*(?:п\.?\s*п\.?|процент)", r"\b90\s*%"),
     r"update\.csv|\[\[S-01\]\]"),
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def score(runs, prefix):
    details = []
    for phase, claim, patterns, source in RUBRIC:
        answer = runs[prefix + phase]["answer"].casefold()
        present = all(re.search(pattern, answer) for pattern in patterns)
        attributed = present and bool(re.search(source, answer, re.IGNORECASE))
        details.append({"claim": claim, "present": present, "attributed": attributed})
    return {
        "present": sum(entry["present"] for entry in details),
        "unattributed": sum(entry["present"] and not entry["attributed"] for entry in details),
        "details": details,
    }


def build(work):
    runs = {phase: read_json(work / f"{phase}-summary.json") for phase in PHASES}
    if len({run["session"] for run in runs.values()}) != len(runs):
        raise ValueError("Сравнение требует четырёх отдельных сессий.")
    if any(runs[name]["tools"] or runs[name].get("other_tools")
           for name in PHASES if name.startswith("baseline-")):
        raise ValueError("Контрольный чат должен пройти без инструментов.")
    case = work / "cases" / "router-live"
    verification = subprocess.check_output(
        [sys.executable, str(ROOT / "strategist.py"), "--cases-dir", str(case.parent),
         "verify", case.name], text=True, encoding="utf-8"
    ).strip()
    state = read_json(case / "state.json")
    artifacts = [item["artifact"] for item in state["checkpoints"]]
    if artifacts != ["brief.md", "research.md", "options.md", "strategy.md", "review-1.md"]:
        raise ValueError("Нужна полная цепочка сохранённых результатов в двух сессиях.")
    for phase, required in (("prepare", {"start", "read", "calculate", "save_stage"}),
                            ("review", {"status", "read", "calculate", "register_update", "save_stage"})):
        if not required <= {item["name"] for item in runs[phase]["tools"] if item["status"] == "completed"}:
            raise ValueError(f"В сессии {phase} отсутствуют подтверждённые вызовы инструментов.")
    audit = {
        "runs": {key: {field: value for field, value in item.items() if field != "session"}
                 for key, item in runs.items()},
        "agent": score(runs, ""),
        "chat": score(runs, "baseline-"),
        "distinct_sessions": len(runs),
        "checkpoint_verification": verification,
        "agent_artifacts": artifacts,
        "impact": state["updates"][-1]["impact"],
        "previous_decision": state["decisions"][-1],
    }
    return audit, case


def report(audit):
    runs = audit["runs"]
    agent, chat = audit["agent"], audit["chat"]
    claims = len(RUBRIC)
    agent_seconds = runs["prepare"]["seconds"] + runs["review"]["seconds"]
    chat_seconds = runs["baseline-prepare"]["seconds"] + runs["baseline-review"]["seconds"]
    result = [
        "# Сравнение на задаче про оплату роутера",
        "",
        "Один парный прогон на одинаковом учебном брифе, таблице и новом замере. "
        "Обе ветки используют Codex с одинаковой настройкой модели. Каждый этап запускается в отдельной сессии. "
        "Сценарий обычного чата получает предыдущий текст в новом запросе. Агент загружает предыдущую стратегию и решение из файлов.",
        "",
        "| Показатель | Агент с MCP | Обычный чат |",
        "|---|---:|---:|",
        f"| Первый этап, секунд | {runs['prepare']['seconds']:.2f} | {runs['baseline-prepare']['seconds']:.2f} |",
        f"| Пересмотр, секунд | {runs['review']['seconds']:.2f} | {runs['baseline-review']['seconds']:.2f} |",
        f"| Найдено утверждений из рубрики | {agent['present']}/{claims} | {chat['present']}/{claims} |",
        f"| Найдено утверждений из рубрики без ссылки | {agent['unattributed']} | {chat['unattributed']} |",
        f"| Проверяемые контрольные точки | {len(audit['agent_artifacts'])} | 0 |",
        "| Перенос предыдущего ответа между сессиями | 0 | 1 |",
        "| Правки текста при переносе | 0 | 0 |",
        "",
        "Время от запуска процесса до записи ответа включает соединение с сервисом, вызовы MCP и проверки разрешений. "
        "Перенос в контрольном сценарии имитирован программно. Время человека на копирование не измерялось. "
        "Оценка ссылок проверяет наличие требуемых чисел и допустимой ссылки в ответе. Рубрика охватывает только четыре заранее выбранных утверждения.",
        "",
        "## Наблюдения",
        "",
        f"В ответах агента найдено {agent['present']} из {claims} утверждений, "
        f"в ответах обычного чата — {chat['present']} из {claims}. "
        f"Общее время двух этапов — {agent_seconds:.2f} с у агента и {chat_seconds:.2f} с у чата. "
        "Агент оставил проверяемые документы, сохранённое решение и карту затронутых выводов. "
        "Для оценки среднего качества или стоимости нужна серия кейсов.",
        "",
        "## Учёт запусков",
        "",
        "| Этап | Входные токены | Из кеша | Выходные | Вызовы MCP с результатом | Ошибки вызовов |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for phase in PHASES:
        run = runs[phase]
        usage = run["usage"]
        valid = sum(tool["status"] == "completed" for tool in run["tools"])
        result.append(f"| {phase} | {usage['input_tokens']} | {usage.get('cached_input_tokens', 0)} | "
                      f"{usage['output_tokens']} | {valid} | {len(run['tools']) - valid} |")
    errors = sum(item["status"] != "completed" for run in runs.values() for item in run["tools"])
    result.extend(["", "Статистика входных токенов включает системный контекст и многократные циклы инструментов. "
                   f"Денежные списания и время автора не измерялись. Ошибки вызовов MCP в этом прогоне — {errors}.", "",
                   "См. [рубрику и трассу](benchmark-results.json), "
                   "[первую стратегию](strategy.md) и [пересмотр](review-1.md)."])
    return "\n".join(result) + "\n"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT / "dist" / "live-router")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    audit, case = build(args.work.resolve())
    text = report(audit)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for artifact in audit["agent_artifacts"]:
            shutil.copy2(case / artifact, args.out / artifact)
        shutil.copy2(case / "evidence.csv", args.out / "evidence.csv")
        (args.out / "benchmark.md").write_text(text, encoding="utf-8")
        (args.out / "benchmark-results.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(text)


if __name__ == "__main__":
    main()
