"""Пакетная проверка единственной основной стратегии на локальных мирах."""
import argparse
import json
from pathlib import Path
from statistics import mean
from time import perf_counter

from factory.planning.weights import load_weights
from factory.runs import RunStore
from factory.simulator import Simulator
from factory.strategy import LearningStrategy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=20)
    parser.add_argument('--horizon', type=int, default=600)
    parser.add_argument('--weights', type=Path, help='JSON с обученными весами Learning v1')
    parser.add_argument('--world-seed', type=int, default=0, help='Первый seed диапазона')
    args = parser.parse_args()
    try:
        strategy = LearningStrategy(load_weights(args.weights)) if args.weights else None
    except (OSError, ValueError, OverflowError) as error:
        parser.error(f'Не удалось загрузить веса: {error}')
    if args.seeds < 1 or args.horizon < 0:
        parser.error('seeds должен быть положительным, horizon — неотрицательным')
    results = []
    max_ms = 0
    for seed in range(args.world_seed, args.world_seed + args.seeds):
        sim, store = Simulator(seed, args.horizon), RunStore(strategy)
        store.start('check', sim.config)
        def decide(payload):
            nonlocal max_ms
            started = perf_counter()
            response = store.event('check', payload)
            max_ms = max(max_ms, (perf_counter() - started) * 1000)
            return response
        result = sim.run(decide)
        results.append(result)
        print(json.dumps(dict(seed=seed, **result)), flush=True)
    invalid = sum(r['invalidCommands'] for r in results)
    print(json.dumps(dict(worlds=len(results),meanProfit=round(mean(r['profit'] for r in results),2),
        minProfit=min(r['profit'] for r in results), maxProfit=max(r['profit'] for r in results),
        invalidCommands=invalid, maxEventMs=round(max_ms,3)), indent=2))
    if invalid:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
