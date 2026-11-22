"""Local case state and evidence checks for the product strategy agent."""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


PROJECT = Path(__file__).resolve().parent
ROOT = PROJECT / "cases"
STAGES = ("brief", "research", "options", "strategy")
KINDS = {"brief_fact", "public_fact", "assumption", "forecast", "synthetic"}
FIELDS = ("id", "kind", "source", "locator", "claim", "limit")
ID = re.compile(r"[A-Z][A-Z0-9]*-\d+")
REFERENCE = re.compile(r"\[\[([A-Z][A-Z0-9]*-\d+)\]\]")
CITATION = re.compile(r"\[\[([^\[\]\r\n]*)\]\]")
DECISION_FIELDS = ("decision", "owner", "metric", "revisit")


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(dump(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def write_lock(case):
    # The OS releases the byte lock when a process exits, including after a crash.
    with (case / "state.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def local_path(case, name):
    path = (case / name).resolve()
    if not path.is_relative_to(case.resolve()):
        raise ValueError("Файл должен находиться внутри папки текущего кейса.")
    return path


def case_path(root, name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", name):
        raise ValueError("ID кейса — строчные латинские буквы, цифры и дефисы.")
    return local_path(root, name)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def evidence_path(case, source):
    if source.startswith(("https://", "http://")):
        if not urlsplit(source).hostname:
            raise ValueError(f"Некорректный URL источника: {source}")
        return None
    if source.startswith("repo:"):
        return local_path(PROJECT, source.removeprefix("repo:"))
    path = local_path(case, source)
    if source == "economics.py" and not path.is_file():
        return PROJECT / source
    return path


def source_fingerprints(case, evidence, refs):
    fingerprints = {}
    by_path = {}
    for key in sorted(refs):
        source = evidence[key]["source"]
        path = evidence_path(case, source)
        if path is None:
            continue
        if not path.is_file():
            raise ValueError(f"Локальный источник {key} отсутствует: {source}")
        if path not in by_path:
            by_path[path] = digest(path)
        fingerprints[key] = by_path[path]
    return fingerprints


def ledger(case):
    with (case / "evidence.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames != list(FIELDS):
            raise ValueError("Неверные колонки реестра evidence.csv.")
        evidence = {}
        for row in rows:
            if None in row or not all(row.values()):
                raise ValueError(f"Неполная запись в строке {rows.line_num} реестра.")
            key = row["id"]
            if not ID.fullmatch(key) or key in evidence or row["kind"] not in KINDS:
                raise ValueError(f"Неверный ID, тип или повтор в строке {rows.line_num}.")
            evidence[key] = row
    return evidence


def references(path, evidence, required=False):
    content = path.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise ValueError(f"Пустой артефакт {path.name}.")
    refs = set(CITATION.findall(content))
    malformed = sorted(key for key in refs if not ID.fullmatch(key))
    if malformed:
        labels = ", ".join(f"[[{key}]]" for key in malformed)
        raise ValueError(f"{path.name}: некорректные ссылки {labels}.")
    unknown = sorted(refs - evidence.keys())
    if unknown:
        raise ValueError(f"{path.name}: неизвестные ссылки {', '.join(unknown)}.")
    if required and not refs:
        raise ValueError(f"В {path.name} нужны ссылки на основания вида [[B-01]].")
    return refs


def initialize(args, case):
    case.mkdir(parents=True, exist_ok=True)
    state_path = case / "state.json"
    with write_lock(case):
        if state_path.exists():
            raise ValueError("Кейс уже существует. Используйте status.")
        if not (case / "evidence.csv").exists():
            (case / "evidence.csv").write_text(",".join(FIELDS) + "\n", encoding="utf-8")
        save_json(state_path, {
            "case": args.case, "request": args.request.strip(),
            "created_at": now(), "checkpoints": [], "decisions": [], "updates": [],
        })


def next_artifact(state):
    complete = {entry["artifact"] for entry in state["checkpoints"]}
    for stage in STAGES:
        if f"{stage}.md" not in complete:
            return f"{stage}.md"
    for entry in state["updates"]:
        if entry["review"] not in complete:
            return entry["review"]
    return None


def source_changes(case, artifact, records, saved_hashes, current_evidence, fingerprint_cache):
    changed = []
    for key, value in records.items():
        if current_evidence.get(key) != value:
            changed.append(f"evidence.csv/{key}")
        source = value["source"]
        target = evidence_path(case, source)
        if target is None:
            continue
        # Some legacy records use descriptive labels instead of paths.
        if (saved_hashes is None and not target.is_file()
                and not source.startswith("repo:") and not re.search(r"[/\\]|\.[A-Za-z0-9]+$", source)):
            continue
        if target not in fingerprint_cache:
            fingerprint_cache[target] = digest(target) if target.is_file() else None
        fingerprint = fingerprint_cache[target]
        finding = f"{artifact} ← {key} ({source})"
        if fingerprint is None:
            changed.append(f"{finding}: источник отсутствует")
        elif saved_hashes is not None and key in saved_hashes and fingerprint != saved_hashes[key]:
            changed.append(f"{finding}: источник изменён")
    return changed


def changed_checkpoints(case, state):
    changed = []
    evidence = ledger(case)
    fingerprint_cache = {}
    for entry in state["checkpoints"]:
        artifact = entry["artifact"]
        path = local_path(case, artifact)
        if not path.is_file() or digest(path) != entry["sha256"]:
            changed.append(artifact)
        changed.extend(source_changes(case, artifact, entry.get("evidence", {}),
                                      entry.get("source_sha256"), evidence, fingerprint_cache))
    for update in state["updates"]:
        key, review = update["id"], update["review"]
        path = local_path(case, update["source_file"])
        if not path.is_file() or digest(path) != update["source_sha256"]:
            changed.append(f"{review} ← {key} ({update['source_file']}): файл обновления отсутствует или изменён")
        if key not in evidence:
            changed.append(f"evidence.csv/{key}")
        records = update.get("evidence", {key: evidence[key]} if key in evidence else {})
        changed.extend(source_changes(case, review, records, update.get("evidence_sha256"),
                                      evidence, fingerprint_cache))
    return list(dict.fromkeys(changed))


def show_status(case, state):
    next_file = next_artifact(state)
    print(dump({
        "case": state["case"], "request": state["request"],
        "completed": [item["artifact"] for item in state["checkpoints"]],
        "next_artifact": next_file,
        "next_action": f"Подготовить {next_file}, затем advance." if next_file else
        "Уточнить выбор человека или ждать новых оснований.",
        "latest_decision": state["decisions"][-1] if state["decisions"] else None,
        "latest_update": state["updates"][-1] if state["updates"] else None,
        "changed_checkpoints": changed_checkpoints(case, state),
    }))


def advance(case, state):
    artifact = next_artifact(state)
    if artifact is None:
        raise ValueError("Все этапы завершены. Используйте status или update.")
    path = local_path(case, artifact)
    evidence = ledger(case)
    refs = references(path, evidence, required=artifact != "brief.md")
    if artifact.startswith("review-"):
        update = next(item for item in state["updates"] if item["review"] == artifact)
        if update["id"] not in refs:
            raise ValueError(f"В пересмотре должна быть ссылка на [[{update['id']}]].")
    state["checkpoints"].append({
        "artifact": artifact, "at": now(), "sha256": digest(path),
        "evidence": {key: evidence[key] for key in sorted(refs)},
        "source_sha256": source_fingerprints(case, evidence, refs),
    })
    return f"Сохранено: {artifact}. Следующий этап: {next_artifact(state) or 'ожидание решения или данных'}."


def record_decision(args, case, state):
    next_file = next_artifact(state)
    if next_file is not None:
        raise ValueError(f"Сначала завершите {next_file}.")
    fields = {key: getattr(args, key).strip() for key in DECISION_FIELDS}
    missing = [f"--{key}" for key, value in fields.items() if not value]
    if missing:
        raise ValueError(f"Поля решения должны быть заполнены: {', '.join(missing)}.")
    state["decisions"].append({
        "at": now(), "artifact": state["checkpoints"][-1]["artifact"],
        **fields, "mode": "simulation" if args.simulated else "confirmed_by_user",
    })
    return dump(state["decisions"][-1])


def add_update(args, case, state):
    if next_artifact(state) is not None:
        raise ValueError("Сначала завершите текущий этап. Дополнения можно внести в текущий черновик.")
    path = local_path(case, args.file)
    update_bytes = path.read_bytes()
    item = json.loads(update_bytes.decode("utf-8-sig"))
    evidence = ledger(case)
    source_id, affects = item.get("id"), item.get("affects")
    if (
        not isinstance(source_id, str) or source_id not in evidence
        or not isinstance(affects, list) or not affects
        or any(not isinstance(key, str) or key not in evidence or key == source_id for key in affects)
        or not isinstance(item.get("summary"), str) or not item["summary"].strip()
    ):
        raise ValueError("Обновление требует id, непустой список affects и summary. ID должны быть в реестре.")
    if any(previous["id"] == source_id for previous in state["updates"]):
        raise ValueError(f"Обновление {source_id} уже зарегистрировано.")
    fingerprints = source_fingerprints(case, evidence, {source_id})
    if ("expected_evidence_sha256" in item
            and item["expected_evidence_sha256"] != fingerprints.get(source_id)):
        raise ValueError(f"Источник {source_id} изменился после расчёта.")
    impact = []
    affected = set(affects)
    for entry in state["checkpoints"]:
        artifact = entry["artifact"]
        for number, line in enumerate(local_path(case, artifact).read_text(encoding="utf-8-sig").splitlines(), 1):
            matched = sorted(set(REFERENCE.findall(line)) & affected)
            if matched:
                impact.append({"file": artifact, "line": number, "refs": matched})
    update = {
        "id": source_id, "affects": affects, "summary": item["summary"],
        "source_file": path.relative_to(case).as_posix(), "source_sha256": hashlib.sha256(update_bytes).hexdigest(),
        "evidence": {source_id: evidence[source_id]},
        "evidence_sha256": fingerprints,
        "review": f"review-{len(state['updates']) + 1}.md", "at": now(), "impact": impact,
    }
    state["updates"].append(update)
    return dump(update)


def verify(case, state):
    changed = changed_checkpoints(case, state)
    if changed:
        raise ValueError(f"Изменены сохранённые артефакты: {', '.join(changed)}.")
    evidence = ledger(case)
    for entry in state["checkpoints"]:
        artifact = entry["artifact"]
        references(local_path(case, artifact), evidence, required=artifact != "brief.md")
    print(f"Проверено: {len(state['checkpoints'])} этапов, {len(evidence)} записей реестра.")


def inspect(args):
    path = Path(args.path)
    suffix = path.suffix.lower()
    if args.limit < 1:
        raise ValueError("Лимит извлечения должен быть положительным.")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise ValueError("Для PDF установите pypdf из requirements-pdf.txt.") from error
        reader = PdfReader(path)
        print(f"Страниц: {len(reader.pages)}")
        for number, page in enumerate(reader.pages[:args.limit], 1):
            print(f"\n--- Страница {number} ---\n{page.extract_text() or '[текст не извлечён]'}")
    elif suffix in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            delimiter = "\t" if suffix == ".tsv" else ","
            for number, row in enumerate(csv.reader(handle, delimiter=delimiter), 1):
                if number > args.limit:
                    print("…")
                    break
                print(f"{number}: {dump(row).replace(chr(10), '')}")
    elif suffix in {".md", ".txt", ".json"}:
        with path.open(encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                if number > args.limit:
                    print("…")
                    break
                print(f"{number}: {line.rstrip()}")
    else:
        raise ValueError("Поддерживаются PDF, CSV, TSV, Markdown, TXT и JSON.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", type=Path, default=ROOT,
                        help="Отдельная папка для рабочих копий кейсов.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "advance", "decide", "update", "verify"):
        command = commands.add_parser(name)
        command.add_argument("case")
        if name == "init":
            command.add_argument("--request", required=True)
        if name == "decide":
            for arg in DECISION_FIELDS:
                command.add_argument(f"--{arg}", required=True)
            command.add_argument("--simulated", action="store_true")
        if name == "update":
            command.add_argument("file")
    inspector = commands.add_parser("inspect")
    inspector.add_argument("path")
    inspector.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            inspect(args)
            return 0
        case = case_path(args.cases_dir, args.case)
        if args.command == "init":
            if not args.request.strip():
                raise ValueError("Запрос не может быть пустым.")
            initialize(args, case)
            print(f"Создан кейс: {args.case}")
            return 0
        state_path = case / "state.json"
        if not state_path.is_file():
            raise ValueError("Кейс не найден. Сначала выполните init.")
        if args.command == "status":
            show_status(case, read_json(state_path))
            return 0
        if args.command == "verify":
            verify(case, read_json(state_path))
            return 0
        with write_lock(case):
            state = read_json(state_path)
            changed = changed_checkpoints(case, state)
            if changed:
                raise ValueError(f"Восстановите контрольные точки перед продолжением: {', '.join(changed)}.")
            if args.command == "advance":
                result = advance(case, state)
            elif args.command == "decide":
                result = record_decision(args, case, state)
            elif args.command == "update":
                result = add_update(args, case, state)
            save_json(state_path, state)
            print(result)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
