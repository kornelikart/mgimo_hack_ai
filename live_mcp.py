"""Local MCP server for the two-session demonstration on a synthetic router case."""

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

from strategist import FIELDS, ROOT, case_path, digest, dump, ledger, local_path, read_json, save_json, write_lock


PROJECT = Path(__file__).resolve().parent
SOURCES = ROOT / "router-payment" / "inputs"
REQUEST = "Стоит ли готовить пилот напоминания об оплате второго номера для роутера?"
RECORDS = (
    ("B-01", "brief_fact", "inputs/brief.md", "§ 1–3", "Согласие и принадлежность обоих номеров обязательны; порог готовности — 90% событий за 24 часа до ограничения.", "Условия учебного сценария"),
    ("B-02", "brief_fact", "inputs/cohorts.csv", "строка 2", "Таблица описывает исходные подходящие события и отключения из-за неоплаты.", "Учебный месяц; эффекта напоминания нет в исходных данных"),
    ("A-01", "assumption", "inputs/brief.md", "§ 3", "Нужно проверить способность биллинга обеспечить своевременность перед пилотом.", "Исходный замер неизвестен"),
)
UPDATE = ("S-01", "synthetic", "inputs/update.csv", "строка 2", "Учебный замер своевременности поступления событий.", "400 событий; нет данных об эффекте напоминания")
SCHEMA_TEXT = {"type": "string", "minLength": 1}


def declared(name, description, properties=None):
    properties = properties or {}
    return {
        "name": name, "description": description,
        "annotations": {"readOnlyHint": name in {"status", "read", "calculate"},
                        "destructiveHint": False, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": properties,
                        "required": list(properties), "additionalProperties": False},
    }


class CaseTools:
    def __init__(self, work, case_id, phase):
        self.work = work
        self.case_id = case_id
        self.case = case_path(work, case_id)
        self.phase = phase
        self.update_input = self.case / "inputs" / "update.csv"
        self.update_pin = self.case / "inputs" / "update-draft.json"
        if phase == "review":
            self.pin_update_source()

    def pin_update_source(self):
        state_file = self.case / "state.json"
        if not state_file.is_file():
            return
        with write_lock(self.case):
            state = read_json(state_file)
            if (state["updates"] or self.update_pin.is_file()
                    or not any(row["artifact"] == "strategy.md" for row in state["checkpoints"])):
                return
            if not self.update_input.is_file():
                shutil.copy2(SOURCES / "update.csv", self.update_input)
            save_json(self.update_pin, {"sha256": digest(self.update_input)})

    def update_snapshot(self):
        contents = self.update_input.read_bytes()
        fingerprint = sha256(contents).hexdigest()
        if self.update_pin.is_file():
            expected = read_json(self.update_pin)["sha256"]
        else:
            updates = read_json(self.case / "state.json")["updates"]
            expected = next((item.get("evidence_sha256", {}).get("S-01")
                             for item in updates if item["id"] == "S-01"), None)
        if expected is not None and expected != fingerprint:
            raise ValueError("Учебный файл update.csv изменился после подготовки снимка.")
        return contents.decode("utf-8-sig"), fingerprint

    def cli(self, command, *args):
        execution = subprocess.run(
            [sys.executable, str(PROJECT / "strategist.py"), "--cases-dir", str(self.work),
             command, self.case_id, *args],
            cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if execution.returncode:
            raise ValueError(execution.stderr.strip())
        return execution.stdout.strip()

    def write_records(self, include_update):
        rows = (UPDATE,) if include_update else RECORDS
        path = self.case / "evidence.csv"
        with write_lock(self.case):
            existing = ledger(self.case)
            additions = io.StringIO()
            writer = csv.writer(additions, lineterminator="\n")
            for row in rows:
                key = row[0]
                if key not in existing:
                    writer.writerow(row)
                elif existing[key] != dict(zip(FIELDS, row)):
                    raise ValueError(f"Запись {key} уже существует с другим содержимым.")
            suffix = additions.getvalue()
            if not suffix:
                return
            contents = path.read_bytes()
            separator = b"" if contents.endswith((b"\r", b"\n")) else b"\n"
            handle = tempfile.NamedTemporaryFile(dir=self.case, prefix="evidence-", suffix=".tmp", delete=False)
            temporary = Path(handle.name)
            try:
                with handle:
                    handle.write(contents + separator + suffix.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def start(self):
        if self.phase != "prepare":
            raise ValueError("Во второй сессии загрузите существующий кейс.")
        if not (self.case / "state.json").is_file():
            self.cli("init", "--request", REQUEST)
        inputs = self.case / "inputs"
        inputs.mkdir(exist_ok=True)
        for name in ("brief.md", "cohorts.csv"):
            target = inputs / name
            if not target.is_file():
                shutil.copy2(SOURCES / name, target)
        if not ledger(self.case):
            self.write_records(include_update=False)
        activity = self.case / "activity.md"
        if not activity.is_file():
            activity.write_text("# Журнал учебного кейса\n", encoding="utf-8")
        return {"status": json.loads(self.cli("status")), "evidence": {row[0]: row[4] for row in RECORDS}}

    def status(self):
        return json.loads(self.cli("status"))

    def read(self, name):
        if name == "knowledge/workflow.md":
            return (PROJECT / name).read_text(encoding="utf-8")
        saved = {"activity.md", "research.md", "options.md", "strategy.md"}
        allowed = saved | {"brief.md", "cohorts.csv", "case/brief.md"}
        if self.phase == "review":
            allowed.add("update.csv")
        if name not in allowed:
            raise ValueError(f"Файл должен входить в текущий этап: {', '.join(sorted(allowed))}.")
        if name in saved:
            return (self.case / name).read_text(encoding="utf-8-sig")
        if name == "case/brief.md":
            return (self.case / "brief.md").read_text(encoding="utf-8-sig")
        if name == "update.csv":
            return self.update_snapshot()[0]
        return (self.case / "inputs" / name).read_text(encoding="utf-8-sig")

    def calculate(self, dataset):
        if dataset == "cohorts":
            row, = csv.DictReader(self.read("cohorts.csv").splitlines())
            numerator = int(row["suspensions_due_to_nonpayment"])
            denominator = int(row["eligible_billing_events"])
            return {"suspensions": numerator, "observations": denominator,
                    "share": numerator / denominator, "evidence": "B-02"}
        if dataset == "update" and self.phase == "review":
            row, = csv.DictReader(self.read("update.csv").splitlines())
            timely = int(row["at_least_24h_early"])
            total = int(row["eligible_events"])
            threshold = 0.90
            return {"timely": timely, "observations": total, "share": timely / total,
                    "threshold": threshold, "gap_pp": round((threshold - timely / total) * 100, 2),
                    "evidence": "S-01"}
        raise ValueError("Этот набор данных недоступен на текущем этапе.")

    def register_update(self, affects, summary):
        if self.phase != "review":
            raise ValueError("Обновление доступно во второй сессии.")
        status = self.status()
        if status["changed_checkpoints"]:
            raise ValueError(f"Проверьте сохранённые источники: {', '.join(status['changed_checkpoints'])}.")
        if status["latest_update"]:
            return status["latest_update"]
        existing = ledger(self.case)
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Опишите, что показывает новый замер.")
        if not isinstance(affects, list) or not affects or any(
            not isinstance(key, str) or key not in existing or key == UPDATE[0] for key in affects
        ):
            raise ValueError("Укажите ID затронутых записей из исходного реестра.")
        _, fingerprint = self.update_snapshot()
        self.write_records(include_update=True)
        relative = "inputs/timing-update.json"
        save_json(self.case / relative, {"id": "S-01", "affects": affects, "summary": summary,
                                         "expected_evidence_sha256": fingerprint})
        return json.loads(self.cli("update", relative))

    def save_stage(self, artifact, markdown):
        status = self.status()
        if artifact != status["next_artifact"] or not artifact:
            raise ValueError(f"Текущий этап требует {status['next_artifact']}.")
        if (self.phase == "review") != artifact.startswith("review-"):
            raise ValueError("Артефакт не относится к текущей сессии.")
        if not markdown.strip():
            raise ValueError("Укажите содержимое артефакта.")
        local_path(self.case, artifact).write_text(markdown.strip() + "\n", encoding="utf-8")
        return {"written": artifact, "checkpoint": self.cli("advance"), "next": self.status()["next_artifact"]}


TOOLS = (
    declared("start", "Создать учебный кейс и вернуть реестр оснований."),
    declared("status", "Загрузить текущий этап, последнее решение, состояние и карту влияния."),
    declared("read", "Прочитать входной файл или сохранённую стратегию.", {"name": SCHEMA_TEXT}),
    declared("calculate", "Посчитать проверяемые доли по cohorts или update.",
             {"dataset": {"type": "string", "enum": ["cohorts", "update"]}}),
    declared("register_update", "Внести обновление и найти зависимые выводы.",
             {"affects": {"type": "array", "items": SCHEMA_TEXT}, "summary": SCHEMA_TEXT}),
    declared("save_stage", "Сохранить самостоятельно написанный Markdown и зафиксировать этап.",
             {"artifact": SCHEMA_TEXT, "markdown": SCHEMA_TEXT}),
)


def dispatch(request, case):
    method = request["method"]
    params = request.get("params", {})
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "strategist", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": list(TOOLS)}
    if method == "tools/call":
        name = params["name"]
        try:
            if name not in {item["name"] for item in TOOLS}:
                raise ValueError("Неизвестный инструмент.")
            value = getattr(case, name)(**params.get("arguments", {}))
            return {"content": [{"type": "text", "text": value if isinstance(value, str) else dump(value)}]}
        except (OSError, ValueError, TypeError) as error:
            return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    raise ValueError("Method not found.")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--phase", choices=("prepare", "review"), required=True)
    args = parser.parse_args()
    case = CaseTools(args.work.resolve(), args.case, args.phase)
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = dispatch(request, case)
        except (KeyError, ValueError) as error:
            response["error"] = {"code": -32601, "message": str(error)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
