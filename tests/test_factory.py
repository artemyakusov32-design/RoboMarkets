import json
import threading
import unittest
from copy import deepcopy
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from factory.config import demo_config
from factory.domain import matches, operation_output
from factory.runs import ApiError, RunStore
from factory.server import make_server
from factory.simulator import Simulator


class RunTests(unittest.TestCase):
    def setUp(self):
        self.sim = Simulator(horizon=60)
        self.store = RunStore()
        self.store.start("test", self.sim.config)

    def payload(self, seq=0):
        return dict(seq=seq, t=0, event=dict(type="idle"), state=self.sim.state(), report=[])

    def test_idempotency_does_not_repeat_strategy(self):
        class Counting:
            def decide(self, config, payload, memory):
                memory["calls"] = memory.get("calls", 0) + 1
                return []
        self.store.strategy = Counting()
        response = self.store.event("test", self.payload())
        self.assertEqual(response, self.store.event("test", self.payload()))
        self.assertEqual(self.store.get("test").memory["calls"], 1)
        self.assertEqual(len(self.store.get("test").log), 1)

    def test_parallel_duplicate_is_atomic(self):
        threads = [threading.Thread(target=self.store.event, args=("test", self.payload())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.get("test").log), 1)

    def test_run_isolation_and_repeat_start(self):
        self.store.get("test").memory["knowledge"] = "private"
        self.store.start("other", self.sim.config)
        self.assertEqual(self.store.get("other").memory, {})
        self.store.start("test", self.sim.config)
        self.assertEqual(self.store.get("test").memory["knowledge"], "private")

    def test_bad_event_does_not_commit(self):
        with self.assertRaises(ApiError):
            self.store.event("test", {"seq": 0})
        self.assertEqual(self.store.get("test").responses, {})
        self.store.event("test", self.payload())

    def test_older_new_seq_rejected(self):
        self.store.event("test", self.payload(2))
        with self.assertRaises(ApiError):
            self.store.event("test", self.payload(1))

    def test_finish_preserves_cached_response(self):
        response = self.store.event("test", self.payload())
        self.store.finish("test", {"profit": 5, "buyers": {"secret": True}})
        self.assertNotIn("buyers", self.store.get("test").result)
        self.assertEqual(response, self.store.event("test", self.payload()))
        with self.assertRaises(ApiError):
            self.store.event("test", self.payload(1))


class SimulatorTests(unittest.TestCase):
    def test_matching_and_inheritance(self):
        item = dict(id="body", kind="body", props=dict(color=None, handles=True))
        self.assertTrue(matches(item, {"kind": "body", "props": {"color": None}}))
        self.assertFalse(matches(item, {"kind": "body", "props": {"color": "*"}}))
        op = demo_config()["machines"][2]["ops"][0]
        output = operation_output(op, [item], "out")
        self.assertEqual(output["props"], dict(color="red", handles=True))

    def test_reservation_and_duplicate_input(self):
        sim = Simulator()
        sheet = sim.new_item("sheet")
        event = dict(type="supply", item=sheet, price=4)
        self.assertIsNone(sim.execute(dict(cmd="accept", itemId=sheet["id"]), event, set()))
        self.assertIsNone(sim.execute(dict(cmd="start", machineId="m-cut", opId="cut", itemIds=[sheet["id"]]), event, set()))
        self.assertEqual(sim.state()["storage"]["free"], 11)
        self.assertEqual(sim.execute(dict(cmd="start", machineId="m-cut", opId="cut", itemIds=[sheet["id"]]), event, set()), "machine_busy")

    def test_refused_offer_costs_money_and_starts_cooldown(self):
        sim = Simulator()
        robot = sim.new_item("robot", {"color": "red", "handles": False})
        sim.items[robot["id"]] = robot
        cmd = dict(cmd="offer", buyerId="b1", itemId=robot["id"], minPrice=100)
        sim.execute(cmd, {"type": "idle"}, set())
        self.assertEqual(sim.cash, -1)
        self.assertEqual(sim.cooldowns["b1"], 8)
        self.assertIn(robot["id"], sim.items)
        sim.execute(cmd, {"type": "idle"}, set())
        self.assertEqual(sim.cash, -2)
        self.assertEqual(sim.pending[-1]["reason"], "cooldown")

    def test_multiple_worlds_have_valid_commands_and_reproduce(self):
        def run(seed):
            sim = Simulator(seed, 180)
            store = RunStore()
            store.start("test", sim.config)
            result = sim.run(lambda p: store.event("test", p))
            self.assertEqual(result["invalidCommands"], 0)
            self.assertGreater(result["sold"], 0)
            self.assertGreaterEqual(sim.state()["storage"]["free"], 0)
            return result
        results = [run(seed) for seed in range(10)]
        self.assertEqual(results[0], run(0))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, auth=True, method=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer test-token"
        data = json.dumps(body).encode() if body is not None else None
        return urlopen(Request(self.url + path, data=data, headers=headers, method=method), timeout=2)

    def test_auth_cors_and_malformed_json(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/runs", auth=False)
        self.assertEqual(error.exception.code, 401)
        with self.request("/v1/health", method="OPTIONS", auth=False) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Private-Network"], "true")
        req = Request(self.url + "/v1/runs/test/event", data=b"{", headers={"Authorization": "Bearer test-token"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req)
        self.assertEqual(error.exception.code, 400)

    def test_full_round_over_http(self):
        sim = Simulator(7, 60)
        def call(action, body):
            with self.request("/v1/runs/test/" + action, body) as response:
                return json.load(response)
        with self.request("/v1/health") as response:
            self.assertEqual(json.load(response), {"status": "ok"})
        self.assertEqual(call("start", sim.config), {"ok": True})
        result = sim.run(lambda p: call("event", p))
        self.assertEqual(result["invalidCommands"], 0)
        self.assertGreater(result["sold"], 0)
        call("finish", result)
        with self.request("/api/runs/test") as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["result"], result)
        with self.request("/", auth=False) as response:
            self.assertIn("Завод роботов", response.read().decode())


if __name__ == "__main__":
    unittest.main()
