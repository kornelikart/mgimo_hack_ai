"""Run the family-internet case calculations from the supplied CSV inputs."""

import argparse
import csv
import json
import math
import sys
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from statistics import NormalDist


INPUTS = Path(__file__).resolve().parent / "cases" / "family-internet" / "inputs"
MONTH_MARGIN_RUB = 500
METRICS = (
    "gross_profit", "active_family_groups", "connected_family_groups",
    "connected_multinumber_owners", "activation_14d",
)
EXPECTED_UNITS = ("RUB", "groups", "groups", "owners", "pct")
ROLL_COLUMNS = (
    "quarter", "offered", "extra_created", "extra_joined", "avoided_group_exits",
    "paid_m1", "paid_m2", "paid_m3", "direct_rub", "development_rub",
    "dec_active_share_pct", "q4_base_created", "q4_extra_activated",
)
PAID_MONTHS = ("paid_m1", "paid_m2", "paid_m3")
CI_CRITICAL = NormalDist().inv_cdf(0.975)
TARGET_POWER = 0.80


def read_rows(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise ValueError(f"Неверные столбцы {path.name}.")
        rows = list(reader)
        if any(None in row or not all(row.values()) for row in rows):
            raise ValueError(f"Неполные строки {path.name}.")
        return rows


def load_baseline(directory):
    rows = read_rows(directory / "baseline.csv", ("metric", "unit", "base_plan", "base_estimate", "without_change", "target"))
    if tuple((row["metric"], row["unit"]) for row in rows) != tuple(zip(METRICS, EXPECTED_UNITS)):
        raise ValueError("Проверьте порядок пяти базовых показателей и их единицы измерения.")
    baseline = {}
    for row in rows:
        amounts = {key: int(row[key]) for key in ("base_plan", "base_estimate", "without_change", "target")}
        if min(amounts.values()) < 0 or (row["unit"] == "pct" and max(amounts.values()) > 100):
            raise ValueError(f"Некорректное значение показателя {row['metric']}.")
        baseline[row["metric"]] = {"unit": row["unit"], **amounts}
    return baseline


def load_rollout(directory):
    scenarios = {}
    for row in read_rows(directory / "rollout.csv", ("scenario", *ROLL_COLUMNS)):
        values = {key: int(row[key]) for key in ROLL_COLUMNS}
        if (
            min(values.values()) < 0 or values["dec_active_share_pct"] > 100
            or not values["extra_joined"] <= values["extra_created"] <= values["offered"]
            or (values["quarter"] != 4 and any(values[key] for key in ROLL_COLUMNS[-3:]))
        ):
            raise ValueError(f"Некорректные пределы в сценарии {row['scenario']}.")
        scenarios.setdefault(row["scenario"], []).append(values)
    if not scenarios:
        raise ValueError("Нужен хотя бы один сценарий.")
    for name, rows in scenarios.items():
        rows.sort(key=lambda row: row["quarter"])
        if [row["quarter"] for row in rows] != [1, 2, 3, 4] or not rows[-1]["q4_base_created"]:
            raise ValueError(f"Нужны четыре отдельных квартала и знаменатель активации для {name}.")
    return scenarios


def wilson_bounds(proportion, sample_size):
    scale = CI_CRITICAL ** 2 / sample_size
    center = (proportion + scale / 2) / (1 + scale)
    width = CI_CRITICAL * math.sqrt((proportion * (1 - proportion) + scale / 4) / sample_size) / (1 + scale)
    return max(0.0, center - width), min(1.0, center + width)


def uplift_interval(control_count, control_n, treatment_count, treatment_n):
    # Newcombe hybrid-score interval based on the two Wilson intervals.
    control = control_count / control_n
    treatment = treatment_count / treatment_n
    effect = control - treatment
    control_low, control_high = wilson_bounds(control, control_n)
    treatment_low, treatment_high = wilson_bounds(treatment, treatment_n)
    lower = effect - math.hypot(control - control_low, treatment_high - treatment)
    upper = effect + math.hypot(control_high - control, treatment - treatment_low)
    return effect, max(-1.0, lower), min(1.0, upper)


def pilot_result(control_n, control_count, treatment_n, treatment_count, minimum_effect, *, planned_per_arm=None):
    if not (control_n > 0 and 0 <= control_count <= control_n and
            treatment_n > 0 and 0 <= treatment_count <= treatment_n):
        raise ValueError("Укажите положительные размеры групп и число событий в пределах групп.")
    effect, lower, upper = uplift_interval(control_count, control_n, treatment_count, treatment_n)
    observed = Fraction(control_count, control_n) - Fraction(treatment_count, treatment_n)
    underfilled = planned_per_arm is not None and min(control_n, treatment_n) < planned_per_arm
    if underfilled:
        outcome = "inconclusive"
    elif observed >= Fraction(str(minimum_effect)) and lower > 0:
        outcome = "statistical_gate_passed"
    elif upper < minimum_effect:
        outcome = "stop_for_insufficient_effect"
    else:
        outcome = "inconclusive"
    result = {"statistical_outcome": outcome, "observed_reduction_pp": round(effect * 100, 3),
              "ci_95_pp": [round(lower * 100, 3), round(upper * 100, 3)],
              "minimum_effect_pp": round(minimum_effect * 100, 4)}
    if planned_per_arm is not None:
        result["planned_groups_per_arm"] = planned_per_arm
        result["recruitment_shortfall"] = {"control": max(0, planned_per_arm - control_n),
                                           "treatment": max(0, planned_per_arm - treatment_n)}
    if underfilled:
        result["reason"] = "sample_under_target"
    return result


def binomial_support(n, rate):
    # Omit tails beyond ten standard deviations, so large plans stay practical.
    mean = n * rate
    radius = max(20, 10 * math.sqrt(mean * (1 - rate)))
    lower, upper = max(0, math.floor(mean - radius)), min(n, math.ceil(mean + radius))
    coefficient = math.lgamma(n + 1)
    log_p, log_q = math.log(rate), math.log1p(-rate)
    masses = [math.exp(coefficient - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                       + k * log_p + (n - k) * log_q) for k in range(lower, upper + 1)]
    return lower, masses


def combined_rule_power(n, control_rate, design_rate, minimum_effect):
    min_count_difference = math.ceil(n * Decimal(str(minimum_effect)))
    first_control, control_masses = binomial_support(n, control_rate)
    first_treatment, treatment_masses = binomial_support(n, design_rate)
    cumulative = [0.0]
    for mass in treatment_masses:
        cumulative.append(cumulative[-1] + mass)
    probability = 0.0
    for control, mass in enumerate(control_masses, first_control):
        # For a fixed control count, worsening treatment counts can only close the lower bound.
        low, high = first_treatment, min(control - min_count_difference,
                                         first_treatment + len(treatment_masses) - 1)
        while low <= high:
            middle = (low + high) // 2
            if uplift_interval(control, n, middle, n)[1] > 0:
                low = middle + 1
            else:
                high = middle - 1
        probability += mass * cumulative[low - first_treatment]
    return min(1.0, probability)


def pilot_size(control_rate=0.12, treatment_rate=0.09, weeks=7, design_rate=None):
    # A true effect at the observed threshold can pass that threshold only about half the time.
    if not 0 < treatment_rate < control_rate < 1 or weeks < 1:
        raise ValueError("Нужны доли 0 < целевая < контроль < 1 и положительное число недель.")
    design_rate = max(treatment_rate / 2, treatment_rate - 0.01) if design_rate is None else design_rate
    if not 0 < design_rate < treatment_rate:
        raise ValueError("Плановая доля должна быть положительной и ниже порога практической пользы.")
    minimum_effect = float(Decimal(str(control_rate)) - Decimal(str(treatment_rate)))
    variance = control_rate * (1 - control_rate) + design_rate * (1 - design_rate)
    z_power = NormalDist().inv_cdf(TARGET_POWER)
    rough_n = max(
        variance * (z_power / (treatment_rate - design_rate)) ** 2,
        variance * ((CI_CRITICAL + z_power) / (control_rate - design_rate)) ** 2,
    )
    # Recruit in blocks of 25 per group, then check both conditions jointly under binomial counts.
    per_arm = max(25, 25 * math.ceil(rough_n / 25))
    probability = combined_rule_power(per_arm, control_rate, design_rate, minimum_effect)
    while probability < TARGET_POWER:
        per_arm += 25
        probability = combined_rule_power(per_arm, control_rate, design_rate, minimum_effect)
    return {"groups_per_arm": per_arm, "total_groups": 2 * per_arm,
            "weekly_recruitment_min": math.ceil(2 * per_arm / weeks), "enrolment_weeks": weeks,
            "control_rate": control_rate, "design_treatment_rate": design_rate,
            "minimum_effect_pp": round(minimum_effect * 100, 4),
            "probability_of_statistical_pass": round(probability, 4)}


def project(rows, baseline):
    base = {key: row["without_change"] for key, row in baseline.items()}
    quarters = []
    cumulative = 0
    invested = 0
    payback = None
    for row in rows:
        months = sum(row[field] for field in PAID_MONTHS)
        contribution = months * MONTH_MARGIN_RUB - row["direct_rub"]
        invested += row["development_rub"]
        cumulative += contribution - row["development_rub"]
        if invested:
            payback = None if cumulative < 0 else (payback or row["quarter"])
        quarters.append({
            "quarter": row["quarter"], "incremental_paid_months": months,
            "gross_profit_delta_rub": contribution, "development_rub": row["development_rub"],
            "cumulative_cash_rub": cumulative,
        })
    delta = sum(row["gross_profit_delta_rub"] for row in quarters)
    last = rows[-1]
    connected = base["connected_family_groups"] + sum(
        row["extra_joined"] + row["avoided_group_exits"] for row in rows
    )
    active = int((Decimal(connected * last["dec_active_share_pct"]) / 100).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    ))
    created = last["q4_base_created"] + last["extra_created"]
    activated = (Decimal(last["q4_base_created"] * base["activation_14d"]) / 100
                 + last["q4_extra_activated"])
    if activated > created:
        raise ValueError("Число активаций больше числа созданных групп.")
    values = (base["gross_profit"] + delta, active, connected, base["connected_multinumber_owners"],
              float((activated * 100 / created).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    return {
        "metrics": {
            key: {"forecast": value, "vs_without_change": round(value - base[key], 2),
                  "shortfall": max(0, round(baseline[key]["target"] - value, 2))}
            for key, value in zip(METRICS, values)
        },
        "quarterly_economics": quarters,
        "direct_costs_rub": sum(row["direct_rub"] for row in rows),
        "paid_months_to_profit_target": max(0, math.ceil(
            (baseline["gross_profit"]["target"] - base["gross_profit"]
             + sum(row["direct_rub"] for row in rows)) / MONTH_MARGIN_RUB
        )),
        "payback_quarter": payback,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=INPUTS)
    parser.add_argument("--control-rate", type=float, default=0.12, help="Базовая доля ограничений (0–1).")
    parser.add_argument("--treatment-rate", type=float, default=0.09, help="Максимальная доля ограничений для практического порога при базовой доле.")
    parser.add_argument("--design-rate", type=float, help="Доля ограничений в плановом сценарии, задающем мощность.")
    parser.add_argument("--weeks", type=int, default=7, help="Число недель набора.")
    parser.add_argument("--results", nargs=4, type=int, metavar=("CONTROL_N", "CONTROL_EVENTS", "TEST_N", "TEST_EVENTS"),
                        help="Проверить статистическое условие на наблюдаемых результатах.")
    args = parser.parse_args()
    try:
        baseline = load_baseline(args.inputs)
        scenarios = {key: project(rows, baseline) for key, rows in load_rollout(args.inputs).items()}
        pilot = pilot_size(args.control_rate, args.treatment_rate, args.weeks, args.design_rate)
        output = {"baseline": baseline, "pilot": pilot, "scenarios": scenarios}
        if args.results:
            minimum_effect = float(Decimal(str(args.control_rate)) - Decimal(str(args.treatment_rate)))
            output["pilot_result"] = pilot_result(
                *args.results, minimum_effect, planned_per_arm=pilot["groups_per_arm"]
            )
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
