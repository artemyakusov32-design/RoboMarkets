"""Подбор 27 коэффициентов Learning v1 по прибыли в локальном симуляторе."""

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from random import Random
from statistics import mean, pstdev
from time import perf_counter

from factory.planning.policy import QWeights
from factory.planning.weights import load_weights, save_weights
from factory.runs import RunStore
from factory.simulator import Simulator
from factory.strategy import LearningStrategy


# Поколения, кандидаты, миры на кандидата, блоки миров, валидация, финальный тест.
PROFILES = {
    "quick": (6, 12, 8, 1, 12, 20),
    "standard": (20, 24, 24, 4, 40, 100),
    "deep": (30, 32, 48, 8, 64, 160),
}
PROFILE_FIELDS = (
    "generations", "population", "train_worlds", "train_batches",
    "validation_worlds", "test_worlds",
)


def time_label(seconds):
    minutes, seconds = divmod(max(0, round(seconds)), 60)
    return f"{minutes} мин {seconds:02} с"


class Progress:
    def __init__(self, args):
        self.started = self.last_update = perf_counter()
        self.completed = 0
        self.train_runs = args.generations * args.population * args.train_worlds
        self.maximum = (self.train_runs + (args.generations + 1) * args.validation_worlds
                        + 2 * args.test_worlds)

    def advance(self, phase):
        self.completed += 1
        now = perf_counter()
        if now - self.last_update >= 15:
            elapsed = now - self.started
            remaining = (self.maximum - self.completed) * elapsed / self.completed
            print(f"{phase}: {self.completed}/{self.maximum} смен, "
                  f"прошло {time_label(elapsed)}, осталось около {time_label(remaining)}",
                  flush=True)
            self.last_update = now


@dataclass(frozen=True)
class WorldResult:
    profit: float
    invalid: int
    timeouts: int


def simulate(job):
    vector, seed, horizon = job
    simulator = Simulator(seed, horizon)
    store = RunStore(LearningStrategy(QWeights.from_vector(vector)))
    store.start("training", simulator.config)
    limit = simulator.config["eventTimeoutMs"] / 1000
    timeouts = 0

    def decide(event):
        nonlocal timeouts
        start = perf_counter()
        response = store.event("training", event)
        timeouts += perf_counter() - start > limit
        return response

    result = simulator.run(decide)
    return WorldResult(result["profit"], result["invalidCommands"], timeouts)


@dataclass(frozen=True)
class Score:
    mean_profit: float
    min_profit: float
    invalid: int
    timeouts: int

    @property
    def eligible(self):
        return self.invalid == 0 and self.timeouts == 0

    def report(self):
        return {
            "meanProfit": round(self.mean_profit, 2),
            "minProfit": self.min_profit,
            "invalidCommands": self.invalid,
            "timeouts": self.timeouts,
        }


def evaluate(vectors, seeds, horizon, pool, progress, phase):
    jobs = ((vector, seed, horizon) for vector in vectors for seed in seeds)
    results = iter(map(simulate, jobs) if pool is None else pool.map(simulate, jobs))
    scores = []
    for _ in vectors:
        worlds = []
        for _ in seeds:
            worlds.append(next(results))
            progress.advance(phase)
        scores.append(Score(
            mean(world.profit for world in worlds),
            min(world.profit for world in worlds),
            sum(world.invalid for world in worlds),
            sum(world.timeouts for world in worlds),
        ))
    return scores


def train(args, pool):
    train_end = args.world_seed + args.train_worlds * args.train_batches
    validation_end = train_end + args.validation_worlds
    train_seeds = range(args.world_seed, train_end)
    validation_seeds = range(train_end, validation_end)
    test_seeds = range(validation_end, validation_end + args.test_worlds)

    progress = Progress(args)

    def score(vectors, seeds, phase):
        return evaluate(vectors, seeds, args.horizon, pool, progress, phase)

    starting_weights = load_weights(args.initial_weights) if args.initial_weights else QWeights()
    initial = starting_weights.to_vector()
    scales = [max(1, abs(weight)) for weight in initial]
    center = [0.0] * len(initial)
    spread = [0.35] * len(initial)
    rng = Random(args.search_seed)
    elite_count = max(2, ceil(args.population * 0.25))
    cma = None
    if args.method == "cma-es":
        try:
            from cma import CMAEvolutionStrategy
        except ImportError as error:
            raise ValueError("Для CMA-ES установите зависимости: "
                             "python3 -m pip install -r requirements-training.txt") from error
        cma = CMAEvolutionStrategy(center, 0.35, {
            "popsize": args.population,
            "seed": args.search_seed,
            "bounds": [-3.0, 3.0],
            "CMA_diagonal": 0,
            "verbose": -9,
            "verb_log": 0,
        })
    print(f"{args.method.upper()} · {len(initial)} весов · {args.workers} процессов · "
          f"{args.generations} поколений × {args.population} кандидатов × "
          f"{args.train_worlds} миров = {progress.train_runs} тренировочных смен; "
          f"до {progress.maximum} смен с проверками", flush=True)
    print(f"Миры: {len(train_seeds)} тренировочных ({args.train_batches} блоков), "
          f"{len(validation_seeds)} проверочных, {len(test_seeds)} финальных. "
          f"Горизонт: {args.horizon} тактов.", flush=True)
    best_vector = initial
    initial_validation, = score([initial], validation_seeds, "Проверка исходных весов")
    best_validation = initial_validation
    if not initial_validation.eligible:
        raise ValueError("Исходные веса не прошли проверку команд или времени")

    print(f"Исходные веса на проверочных мирах: {initial_validation.mean_profit:.2f}", flush=True)
    for generation in range(1, args.generations + 1):
        if cma is not None:
            offsets = cma.ask()
        else:
            offsets = [center.copy()] + [
                [max(-3, min(3, rng.gauss(mu, sigma))) for mu, sigma in zip(center, spread)]
                for _ in range(args.population - 1)
            ]
        vectors = [
            [float(weight + offset * scale) for weight, offset, scale in zip(initial, row, scales)]
            for row in offsets
        ]
        batch = (generation - 1) % args.train_batches
        batch_start = batch * args.train_worlds
        generation_seeds = train_seeds[batch_start:batch_start + args.train_worlds]
        phase = f"Поколение {generation}/{args.generations}"
        scores = score(vectors, generation_seeds, phase)
        ranked = sorted(
            (i for i, score in enumerate(scores) if score.eligible),
            key=lambda i: scores[i].mean_profit, reverse=True,
        )
        if not ranked:
            print(f"{phase}: нет допустимых кандидатов ({progress.completed} смен)", flush=True)
            continue
        champion = ranked[0]
        validation, = score([vectors[champion]], validation_seeds, f"{phase}, проверка")
        if validation.eligible and validation.mean_profit > best_validation.mean_profit:
            best_vector, best_validation = vectors[champion], validation

        if cma is not None:
            # Подбираем новое распределение, когда все основные родители допустимы.
            if len(ranked) >= args.population // 2:
                invalid_objective = max(-scores[i].mean_profit for i in ranked) + 1.0
                cma.tell(offsets, [
                    -result.mean_profit if result.eligible else invalid_objective
                    for result in scores
                ])
        elif len(ranked) >= 2:
            for i, column in enumerate(zip(*(offsets[j] for j in ranked[:elite_count]))):
                center[i] = 0.3 * center[i] + 0.7 * mean(column)
                spread[i] = max(0.05, 0.3 * spread[i] + 0.7 * pstdev(column))
        print(f"{phase}: "
              f"обучение {scores[champion].mean_profit:.2f}, "
              f"проверка {validation.mean_profit:.2f}, "
              f"лучшая проверка {best_validation.mean_profit:.2f} · "
              f"{progress.completed}/{progress.maximum} смен", flush=True)

    initial_test, = score([initial], test_seeds, "Финальный тест исходных весов")
    if not initial_test.eligible:
        raise ValueError("Исходные веса не прошли финальную проверку команд или времени")
    candidate_test = None
    if best_vector != initial:
        candidate_test, = score([best_vector], test_seeds, "Финальный тест кандидата")
    accepted = (
        candidate_test is not None and candidate_test.eligible
        and candidate_test.mean_profit > initial_test.mean_profit
    )
    report = {
        "method": args.method.upper(),
        "profile": args.profile,
        "initialWeights": args.initial_weights.name if args.initial_weights else "default",
        "initialCoefficients": starting_weights.w,
        "workers": args.workers,
        "trainWorldsPerCandidate": args.train_worlds,
        "trainBatches": args.train_batches,
        "trainingRuns": progress.train_runs,
        "completedRuns": progress.completed,
        "maxPlannedRuns": progress.maximum,
        "elapsedSeconds": round(perf_counter() - progress.started, 2),
        "searchSeed": args.search_seed,
        "population": args.population,
        "generations": args.generations,
        "horizon": args.horizon,
        "worlds": {
            split: {"firstSeed": seeds.start, "count": len(seeds)}
            for split, seeds in (
                ("train", train_seeds), ("validation", validation_seeds), ("test", test_seeds)
            )
        },
        "initialValidation": initial_validation.report(),
        "bestValidation": best_validation.report(),
        "initialTest": initial_test.report(),
        "candidateTest": candidate_test.report() if candidate_test is not None else None,
        "accepted": accepted,
    }
    return QWeights.from_vector(best_vector if accepted else initial), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("cma-es", "cem"), default="cma-es")
    parser.add_argument("--profile", choices=PROFILES,
                        help="Объём обучения. По умолчанию standard для CMA-ES, quick для CEM")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--generations", type=int)
    parser.add_argument("--population", type=int)
    parser.add_argument("--train-worlds", type=int, help="Число миров на кандидата за поколение")
    parser.add_argument("--train-batches", type=int, help="Число непересекающихся блоков миров")
    parser.add_argument("--validation-worlds", type=int)
    parser.add_argument("--test-worlds", type=int)
    parser.add_argument("--world-seed", type=int, default=1000)
    parser.add_argument("--search-seed", type=int, default=1)
    parser.add_argument("--horizon", type=int, default=600)
    parser.add_argument("--initial-weights", type=Path, help="Начать с ранее сохранённых весов")
    parser.add_argument("--output", type=Path, help="Куда сохранить веса и отчёт")
    args = parser.parse_args()
    args.profile = args.profile or ("standard" if args.method == "cma-es" else "quick")
    for field, value in zip(PROFILE_FIELDS, PROFILES[args.profile]):
        if getattr(args, field) is None:
            setattr(args, field, value)
    if args.output is None:
        filename = "learning-weights-cma-es.json" if args.method == "cma-es" else "learning-weights.json"
        args.output = Path("work") / filename
    if args.initial_weights and args.initial_weights.resolve() == args.output.resolve():
        parser.error("Укажите разные пути исходных весов и результата")
    if min(args.workers, args.horizon, *(getattr(args, f) for f in PROFILE_FIELDS)) < 1:
        parser.error("Число процессов, поколений, миров и тактов должно быть положительным")
    if args.population < 4 or args.world_seed < 0:
        parser.error("Нужно минимум 4 кандидата и неотрицательное начало диапазона миров")
    if args.train_batches > args.generations:
        parser.error("Число тренировочных блоков не должно превышать число поколений")
    if args.method == "cma-es" and not 1 <= args.search_seed < 2**32:
        parser.error("Для CMA-ES search-seed должен быть от 1 до 4294967295")

    try:
        executor = (ProcessPoolExecutor(max_workers=args.workers)
                    if args.workers > 1 else nullcontext())
        with executor as pool:
            weights, report = train(args, pool)
        save_weights(args.output, weights, report)
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(1, f"Ошибка обучения: {error}\n")

    print(f"Финальный тест исходных весов: {report['initialTest']['meanProfit']:.2f}")
    if report["candidateTest"] is not None:
        print(f"Финальный тест кандидата: {report['candidateTest']['meanProfit']:.2f}")
    selection = "обученные веса" if report["accepted"] else "исходные веса"
    print(f"Рассчитано {report['completedRuns']} смен за {time_label(report['elapsedSeconds'])}.")
    print(f"Сохранены {selection}: {args.output}")


if __name__ == "__main__":
    main()
