"""Reproduce checkpoints and the review in a working copy of the family case."""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
CASE = "family-internet"
SOURCE = HERE / "cases" / CASE
UPDATED_SOURCE = "timing-update.json"
REQUEST = "Кому помочь со связью в первую очередь и одобрять ли пилот к концу Q1?"
UPDATE_ID = "S-01"
STAGES = ("brief.md", "research.md", "options.md", "strategy.md")


def agent(root, *arguments, capture=False):
    command, *rest = arguments
    return subprocess.run(
        [sys.executable, str(HERE / "strategist.py"), "--cases-dir", str(root),
         command, CASE, *rest], check=True, text=True, encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def seed_evidence(destination, include_update):
    target = destination / "evidence.csv"
    temporary = target.with_name("evidence-tmp.csv")
    with (SOURCE / "evidence.csv").open(encoding="utf-8", newline="") as seed:
        rows = csv.DictReader(seed)
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows.fieldnames)
            writer.writeheader()
            writer.writerows(row for row in rows if include_update or row["id"] != UPDATE_ID)
    os.replace(temporary, target)


def prepare(root, case):
    if not (case / "state.json").exists():
        agent(root, "init", "--request", REQUEST)
    status = json.loads(agent(root, "status", capture=True))
    if all(stage in status["completed"] for stage in STAGES):
        if not status["latest_decision"] and not status["latest_update"]:
            decide(root, "prepare")
        return
    (case / "inputs").mkdir(exist_ok=True)
    for path in (SOURCE / "inputs").iterdir():
        if path.name != UPDATED_SOURCE and path.is_file():
            shutil.copy2(path, case / "inputs" / path.name)
    seed_evidence(case, include_update=False)
    for document in STAGES:
        if document in status["completed"]:
            continue
        print(f"Этап: {document}", flush=True)
        shutil.copy2(SOURCE / document, case / document)
        agent(root, "advance")
    decide(root, "prepare")


def decide(root, phase):
    if phase == "prepare":
        decision = "Проверить готовность T1 к пилоту. Лимит подготовки 180 000 ₽."
        revisit = "Конец Q1: права, набор, минимум 90% своевременных сигналов и p95 ≤15 мин."
    else:
        decision = "Отложить пилот на T1. Проверить источник задержки в течение двух недель."
        revisit = "Повторный замер: ≥90% своевременных событий и p95 ≤15 мин; проверка набора и прав."
    agent(root, "decide", "--simulated", "--decision", decision, "--owner", "Руководитель продукта",
          "--metric", "Доля ограничений из-за исчерпания пакета за 7 дней", "--revisit", revisit)


def review(root, case):
    status = json.loads(agent(root, "status", capture=True))
    if "strategy.md" not in status["completed"] or status["latest_decision"] is None:
        raise ValueError("Сначала выполните prepare.")
    if status["latest_update"] is None:
        seed_evidence(case, include_update=True)
        shutil.copy2(SOURCE / "inputs" / UPDATED_SOURCE, case / "inputs" / UPDATED_SOURCE)
        agent(root, "update", "inputs/" + UPDATED_SOURCE)
        status = json.loads(agent(root, "status", capture=True))
    review_file = status["latest_update"]["review"]
    if review_file not in status["completed"]:
        shutil.copy2(SOURCE / review_file, case / review_file)
        agent(root, "advance")
    if status["latest_decision"]["artifact"] != review_file:
        decide(root, "review")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "review", "status"))
    parser.add_argument("--work", type=Path, default=HERE / "dist" / "demo-cases")
    args = parser.parse_args()
    root = args.work.resolve()
    if root == SOURCE.parent or root == SOURCE or root.is_relative_to(SOURCE):
        parser.error("Папка повтора должна находиться вне исходного кейса.")
    case = root / CASE
    try:
        if args.phase == "prepare":
            prepare(root, case)
        elif args.phase == "review":
            review(root, case)
        agent(root, "status")
        agent(root, "verify")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
