"""Запуск локальной тестовой смены для веб-панели. Не вызывается ареной."""
import logging
import math
from threading import Event, Lock, Thread
from uuid import uuid4

from .config import demo_config
from .runs import ApiError
from .simulator import Simulator

log = logging.getLogger(__name__)


class DemoRunner:
    def __init__(self, store):
        self.store = store
        self.lock = Lock()
        self.stop = Event()
        self.active = False
        self.thread = None

    def start(self, body):
        seed, horizon, delay = body.get('seed', 1), body.get('horizon', 600), body.get('delay', .02)
        if (type(seed) is not int or not 0 <= seed <= 2**31 - 1
                or type(horizon) is not int or not 1 <= horizon <= 2000
                or type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= .2):
            raise ApiError('bad_demo', 'seed: 0…2147483647, horizon: 1…2000, delay: 0…0.2')
        with self.lock:
            if self.active:
                raise ApiError('demo_busy', 'Тестовая смена уже идёт. Дождитесь завершения.', 409)
            if self.stop.is_set():
                raise ApiError('shutting_down', 'Сервис останавливается', 503)
            run_id = 'demo-s{}-{}'.format(seed, uuid4().hex[:8])
            sim = Simulator(seed, horizon)
            self.store.start(run_id, dict(sim.config, runId=run_id))
            self.active = True
            self.thread = Thread(target=self._run, args=(run_id, sim, delay), daemon=True)
            self.thread.start()
        return dict(ok=True, runId=run_id)

    def _run(self, run_id, sim, delay):
        class Cancelled(Exception):
            pass
        def decide(payload):
            if self.stop.is_set():
                raise Cancelled()
            response = self.store.event(run_id, payload)
            if delay and self.stop.wait(delay):
                raise Cancelled()
            return response
        try:
            self.store.finish(run_id, sim.run(decide))
        except Cancelled:
            self.store.finish(run_id, dict(cancelled=True, profit=round(sim.cash, 2)))
        except Exception:
            log.exception('Demo %s failed', run_id)
            self.store.finish(run_id, dict(failed=True, profit=round(sim.cash, 2)))
        finally:
            with self.lock:
                self.active = False

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
