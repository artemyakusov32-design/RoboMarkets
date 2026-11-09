"""Полный прогон через настоящее HTTP API или напрямую без сервера."""
import argparse
import json
import os
import time
import uuid
from urllib.request import Request, urlopen

from factory.runs import RunStore
from factory.simulator import Simulator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Например http://127.0.0.1:4001; без URL запуск напрямую")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--horizon", type=int, default=600)
    parser.add_argument("--delay", type=float, default=0, help="Пауза между событиями в секундах")
    args = parser.parse_args()
    sim = Simulator(args.seed, args.horizon)
    store = RunStore()
    run_id = "demo-" + uuid.uuid4().hex[:10]

    def call(action, body):
        if not args.url:
            return getattr(store, action)(run_id, body)
        headers = {"Content-Type": "application/json", "X-Arena-Spec": "v1", "X-Arena-Mode": "practice"}
        if os.environ.get("ARENA_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["ARENA_TOKEN"]
        if "seq" in body:
            headers["X-Arena-Seq"] = str(body["seq"])
        req = Request(args.url.rstrip("/") + "/v1/runs/" + run_id + "/" + action,
            data=json.dumps(body).encode(), headers=headers, method="POST")
        with urlopen(req, timeout=sim.config["eventTimeoutMs"] / 1000) as response:
            return json.load(response)

    def decide(payload):
        response = call("event", payload)
        if args.delay:
            time.sleep(args.delay)
        return response

    call("start", dict(sim.config, runId=run_id))
    result = sim.run(decide)
    call("finish", result)
    print(json.dumps(dict(runId=run_id, **result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
