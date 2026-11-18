import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factory.planning.policy import DEFAULT_Q, QWeights
from factory.planning.weights import load_weights, save_weights
from factory.runs import RunStore
from factory.simulator import Simulator
from factory.strategy import LearningStrategy
from scripts import check_worlds


class WeightFileTests(unittest.TestCase):
    def test_loaded_weights_reach_the_planner(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "weights.json"
            save_weights(path, QWeights(dict.fromkeys(DEFAULT_Q, 0.0)), {"accepted": True})
            strategy = LearningStrategy(load_weights(path))

        def run(store):
            simulator = Simulator(1, 60)
            store.start("test", simulator.config)
            return simulator.run(lambda event: store.event("test", event))

        self.assertEqual(run(RunStore(strategy))["sold"], 0)
        self.assertGreater(run(RunStore())["sold"], 0)

    def test_check_worlds_uses_weights_from_cli(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "weights.json"
            save_weights(path, QWeights(dict.fromkeys(DEFAULT_Q, 0.0)), {})
            output = io.StringIO()
            args = ["check_worlds", "--weights", str(path), "--seeds", "1", "--horizon", "60"]
            with patch("sys.argv", args), redirect_stdout(output):
                check_worlds.main()

        first_world = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(first_world["sold"], 0)
        self.assertEqual(first_world["profit"], 0)

    def test_damaged_weight_file_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "weights.json"
            for weights in ({}, dict(DEFAULT_Q, of_fee=float("nan")), dict(DEFAULT_Q, of_fee=True)):
                with self.subTest(weights=weights):
                    path.write_text(json.dumps({"version": 1, "weights": weights}))
                    with self.assertRaises(ValueError):
                        load_weights(path)


class TrainingVolumeTests(unittest.TestCase):
    def test_training_rotates_worlds_and_keeps_holdouts_separate(self):
        from argparse import Namespace

        from scripts import train_weights

        args = Namespace(method="cem", profile="quick", initial_weights=None, workers=1,
                         generations=2, population=4, train_worlds=2, train_batches=2,
                         validation_worlds=2, test_worlds=2, world_seed=1000,
                         search_seed=1, horizon=600)
        initial = QWeights().to_vector()

        def simulate(job):
            vector, _, _ = job
            return train_weights.WorldResult(0 if vector == initial else 1, 0, 0)

        with patch.object(train_weights, "simulate", side_effect=simulate) as simulation:
            with redirect_stdout(io.StringIO()):
                _, report = train_weights.train(args, pool=None)

        seeds = [call.args[0][1] for call in simulation.call_args_list]
        self.assertEqual(seeds[:2], [1004, 1005])
        self.assertEqual(seeds[2:10], [1000, 1001] * 4)
        self.assertEqual(seeds[12:20], [1002, 1003] * 4)
        self.assertEqual(seeds[-4:], [1006, 1007] * 2)
        self.assertEqual(report["trainingRuns"], 16)
        self.assertEqual(report["completedRuns"], len(seeds))
        self.assertTrue(report["accepted"])
