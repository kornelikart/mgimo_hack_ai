import csv
import io
import json
import shutil
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

import economics
import benchmark
import live_mcp
import live_run
from live_mcp import CaseTools
import strategist


class SourceChecks(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.case = Path(self.directory.name)
        self.source = self.case / "baseline.csv"
        self.source.write_text("cohort,count\nA,42\n", encoding="utf-8")
        with (self.case / "evidence.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(strategist.FIELDS)
            writer.writerow(("B-01", "brief_fact", "baseline.csv", "2", "42", "учебная"))
        (self.case / "brief.md").write_text("Пример на [[B-01]].", encoding="utf-8")
        self.state = {"checkpoints": [], "updates": []}
        strategist.advance(self.case, self.state)

    def test_source_change_and_missing_source_report_affected_artifact(self):
        self.assertEqual(strategist.changed_checkpoints(self.case, self.state), [])
        self.source.write_text("cohort,count\nA,99\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"brief\.md ← B-01.*изменён"):
            strategist.verify(self.case, self.state)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, r"brief\.md ← B-01.*отсутствует"):
            strategist.verify(self.case, self.state)

    def test_legacy_case_without_hashes_detects_missing_source(self):
        self.state["checkpoints"][0].pop("source_sha256")
        self.assertEqual(strategist.changed_checkpoints(self.case, self.state), [])
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, r"brief\.md ← B-01.*отсутствует"):
            strategist.verify(self.case, self.state)

    def test_legacy_text_label_does_not_require_a_file(self):
        snapshot = self.state["checkpoints"][0]
        snapshot.pop("source_sha256")
        snapshot["evidence"]["B-01"]["source"] = "Скриншот 2"
        with (self.case / "evidence.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, strategist.FIELDS)
            writer.writeheader()
            writer.writerow(snapshot["evidence"]["B-01"])
        self.assertEqual(strategist.changed_checkpoints(self.case, self.state), [])

    def test_malformed_citation_is_rejected_even_with_valid_citation(self):
        (self.case / "research.md").write_text("Данные [[B-01]], опечатка [[B-XX]].", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"некорректные ссылки.*\[\[B-XX\]\]"):
            strategist.advance(self.case, self.state)
        self.assertEqual(strategist.next_artifact(self.state), "research.md")


class PilotChecks(unittest.TestCase):
    def test_plan_uses_joint_rule(self):
        pilot = economics.pilot_size()
        self.assertEqual(pilot["total_groups"], 2600)
        self.assertGreaterEqual(pilot["probability_of_statistical_pass"], .80)
        self.assertLess(economics.combined_rule_power(1300, .12, .09, .03), .55)

    def test_statistical_decisions(self):
        for observed, label in ((117, "statistical_gate_passed"), (133, "inconclusive"),
                                (150, "stop_for_insufficient_effect")):
            with self.subTest(observed=observed):
                outcome = economics.pilot_result(1300, 156, 1300, observed, .03)
                self.assertEqual(outcome["statistical_outcome"], label)

    def test_confidence_interval_is_not_degenerate_at_endpoints(self):
        result = economics.pilot_result(1, 1, 1, 0, .03)
        self.assertEqual(result["statistical_outcome"], "inconclusive")
        self.assertLess(result["ci_95_pp"][0], 0)

    def test_pilot_defers_when_either_group_is_under_target(self):
        result = economics.pilot_result(1300, 156, 100, 0, .03, planned_per_arm=1300)
        self.assertEqual(result["statistical_outcome"], "inconclusive")
        self.assertEqual(result["reason"], "sample_under_target")
        self.assertEqual(result["recruitment_shortfall"], {"control": 0, "treatment": 1200})


class BenchmarkChecks(unittest.TestCase):
    def test_summary_reflects_changed_results(self):
        data = benchmark.read_json(
            benchmark.ROOT / "cases" / "router-payment" / "results" / "benchmark-results.json")
        data["agent"]["present"] = 1
        for name, run in data["runs"].items():
            run["seconds"] = 3 if name.startswith("baseline-") else 1
            run["tools"] = []
        report = benchmark.report(data)
        self.assertIn("В ответах агента найдено 1 из 4", report)
        self.assertIn("2.00 с у агента и 6.00 с у чата", report)
        self.assertIn("Ошибки вызовов MCP в этом прогоне — 0", report)


class LiveToolChecks(unittest.TestCase):
    def test_inputs_are_phase_bound_and_resume_reads_saved_work(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            first = CaseTools(work, "router", "prepare")
            first.start()
            self.assertEqual(first.calculate("cohorts")["share"], .15)
            self.assertIn("Журнал", first.read("activity.md"))
            with self.assertRaisesRegex(ValueError, "текущий этап"):
                first.read("update.csv")
            for phase in ("brief", "research", "options", "strategy"):
                first.save_stage(f"{phase}.md", f"{phase} [[A-01]].")
            resumed = CaseTools(work, "router", "review")
            self.assertIn("research", resumed.read("research.md"))
            self.assertIn("Журнал", resumed.read("activity.md"))
            self.assertEqual(resumed.calculate("update")["share"], .70)
            self.assertTrue(resumed.register_update(["A-01"], "70% при пороге 90%.")["impact"])
            resumed.save_stage("review-1.md", "Отложить пилот. [[S-01]]")
            self.assertIn("5 этапов", resumed.cli("verify"))

    def test_review_append_preserves_manually_added_evidence(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            first = CaseTools(work, "router", "prepare")
            first.start()
            path = first.case / "evidence.csv"
            with path.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(("B-99", "brief_fact", "inputs/brief.md",
                                             "ручная заметка", "Сопоставлен владелец", "учебный источник"))
            for phase in ("brief", "research", "options", "strategy"):
                first.save_stage(f"{phase}.md", f"{phase} [[A-01]] [[B-99]].")
            before = path.read_bytes()
            resumed = CaseTools(work, "router", "review")
            with self.assertRaisesRegex(ValueError, "Опишите"):
                resumed.register_update(["B-99"], " ")
            self.assertEqual(path.read_bytes(), before)
            update = resumed.register_update(["B-99"], "70% при пороге 90%.")
            self.assertTrue(path.read_bytes().startswith(before))
            self.assertTrue(any("B-99" in item["refs"] for item in update["impact"]))
            resumed.save_stage("review-1.md", "Проверить сигнал. [[S-01]] [[B-99]]")
            self.assertIn("5 записей", resumed.cli("verify"))

    def test_pending_review_rejects_changed_source(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            first = CaseTools(work, "router", "prepare")
            first.start()
            for phase in ("brief", "research", "options", "strategy"):
                first.save_stage(f"{phase}.md", f"{phase} [[A-01]].")
            resumed = CaseTools(work, "router", "review")
            resumed.register_update(["A-01"], "70% при пороге 90%.")
            source = resumed.case / "inputs" / "update.csv"
            original = source.read_bytes()
            source.write_bytes(original.replace(b"280", b"281"))
            resumed.update_pin.unlink()
            with self.assertRaisesRegex(ValueError, "update.csv изменился"):
                resumed.calculate("update")
            expected = r"review-1\.md ← S-01.*update\.csv.*источник изменён"
            with self.assertRaisesRegex(ValueError, expected):
                resumed.cli("verify")
            with self.assertRaisesRegex(ValueError, expected):
                resumed.save_stage("review-1.md", "Разобрать задержку. [[S-01]]")
            self.assertEqual(resumed.status()["next_artifact"], "review-1.md")
            source.write_bytes(original)
            update = resumed.case / "inputs" / "timing-update.json"
            description = update.read_bytes()
            update.write_bytes(description + b"\n")
            self.assertTrue(any("файл обновления" in item
                                for item in resumed.status()["changed_checkpoints"]))
            update.write_bytes(description)
            resumed.save_stage("review-1.md", "Разобрать задержку. [[S-01]]")
            self.assertIn("5 этапов", resumed.cli("verify"))

    def test_decision_requires_all_fields(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            case = CaseTools(work, "router", "prepare")
            case.start()
            for phase in ("brief", "research", "options", "strategy"):
                case.save_stage(f"{phase}.md", f"{phase} [[A-01]].")
            with self.assertRaisesRegex(ValueError, r"Поля решения.*--metric"):
                case.cli("decide", "--decision", "Проверка", "--owner", "Аналитик",
                         "--metric", " ", "--revisit", "После замера", "--simulated")
            self.assertIsNone(case.status()["latest_decision"])

    def test_review_pins_input_before_calculation(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            fixture = work / "fixture"
            shutil.copytree(live_mcp.SOURCES, fixture)
            with patch.object(live_mcp, "SOURCES", fixture):
                first = CaseTools(work, "router", "prepare")
                first.start()
                for phase in ("brief", "research", "options", "strategy"):
                    first.save_stage(f"{phase}.md", f"{phase} [[A-01]].")
                review = CaseTools(work, "router", "review")
                self.assertEqual(review.calculate("update")["timely"], 280)
                fixture_input = fixture / "update.csv"
                fixture_input.write_bytes(fixture_input.read_bytes().replace(b"280", b"360"))
                staged = review.update_input.read_bytes()
                review.update_input.write_bytes(staged.replace(b"280", b"310"))
                with self.assertRaisesRegex(ValueError, "update.csv изменился"):
                    review.register_update(["A-01"], "70% при пороге 90%.")
                self.assertIsNone(review.status()["latest_update"])
                review.update_input.write_bytes(staged)
                original_cli = review.cli

                def change_before_registration(command, *args):
                    if command == "update":
                        review.update_input.write_bytes(staged.replace(b"280", b"310"))
                    return original_cli(command, *args)

                with patch.object(review, "cli", side_effect=change_before_registration):
                    with self.assertRaisesRegex(ValueError, "Источник S-01 изменился после расчёта"):
                        review.register_update(["A-01"], "70% при пороге 90%.")
                self.assertIsNone(review.status()["latest_update"])
                review.update_input.write_bytes(staged)
                review.register_update(["A-01"], "70% при пороге 90%.")
                self.assertEqual(review.calculate("update")["timely"], 280)
                self.assertEqual(review.update_input.read_bytes(), staged)


class RunnerChecks(unittest.TestCase):
    def test_restart_completes_missing_stages_and_keeps_previous_log(self):
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()
            case = CaseTools(work / "cases", live_run.CASE, "prepare")
            case.start()
            case.save_stage("brief.md", "Сохранённый бриф.")
            self.assertEqual(case.read("case/brief.md").strip(), "Сохранённый бриф.")
            (work / "prepare.jsonl").write_text("старый журнал\n", encoding="utf-8")
            native_run = live_run.subprocess.run
            prompts = []

            def run(arguments, **options):
                if arguments[0] != "codex":
                    return native_run(arguments, **options)
                prompts.append(options["input"].decode("utf-8"))
                events = [{"type": "thread.started", "thread_id": "test-resumed"}]
                for name in ("research.md", "options.md", "strategy.md"):
                    case.save_stage(name, "Продолжение по данным [[A-01]].")
                    events.append({"type": "item.completed", "item": {"type": "mcp_tool_call",
                                    "server": "strategist", "tool": "save_stage", "status": "completed"}})
                events.extend([
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "Готово."}},
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
                ])
                options["stdout"].write("".join(json.dumps(event) + "\n" for event in events).encode())

            with patch.object(live_run.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
                live_run.codex_run("prepare", work)
                live_run.codex_run("prepare", work)
            self.assertEqual(len(prompts), 1)
            self.assertIn("осталось сохранить: research.md, options.md, strategy.md", prompts[0])
            self.assertIn("На диске уже сохранены: brief.md", prompts[0])
            self.assertEqual(live_run.saved_artifacts(work), live_run.ARTIFACTS["prepare"])
            summary = json.loads((work / "prepare-summary.json").read_text(encoding="utf-8"))
            previous = work / summary["previous_attempt"] / "prepare.jsonl"
            self.assertEqual(previous.read_text(encoding="utf-8"), "старый журнал\n")
            self.assertEqual(summary["resumed_from"], ["brief.md"])


if __name__ == "__main__":
    unittest.main()
