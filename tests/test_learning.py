import unittest
from copy import deepcopy

from factory.config import demo_config
from factory.demo import DemoRunner
from factory.runs import ApiError
from factory.planning.controller import LearningController, Pending
from factory.planning.market import MarketModel
from factory.planning.policy import Ctx
from factory.planning.schemas import StartMsg, EventMsg
from factory.runs import RunStore
from factory.simulator import Simulator


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.sim = Simulator(1, 600)
        self.cfg = StartMsg.from_wire(self.sim.config)
        self.bot = LearningController(self.cfg)

    def msg(self, t=0, event=None, seq=0, report=None):
        return EventMsg.from_wire(dict(seq=seq, t=t, event=event or dict(type='idle'),
            state=self.sim.state(), report=report or []))

    def test_sale_updates_related_types(self):
        market = MarketModel(self.cfg.buyers, self.cfg.attributes, 100)
        market.on_sale_result('b1', ('red', False), 'sold', 60, None, 40)
        self.assertEqual(market.beliefs['b1'].ideal_type(), ('red', False))
        self.assertEqual(market.interval('b1', ('blue', True)), (0, 0))
        self.assertGreater(market.expected_price('b1', ('blue', False)), 0)

    def test_known_buyer_gets_reserve_price_even_after_high_sale(self):
        robot_type = ('red', False)
        for buyer in self.cfg.buyers:
            preferred = robot_type if buyer == 'b1' else ('blue', True)
            self.bot.market.on_sale_result(buyer, preferred, 'sold', 70, None, 10)
        self.sim.items['robot'] = dict(id='robot', kind='robot',
                                      props=dict(color='red', handles=False))

        offers = [cmd for cmd in self.bot.handle_event(self.msg()) if cmd['cmd'] == 'offer']

        self.assertEqual([(cmd['buyerId'], cmd['minPrice']) for cmd in offers], [('b1', 10)])

    def test_slot_plan_counts_existing_robot_against_its_best_buyer(self):
        self.cfg = StartMsg.from_wire(demo_config(horizon=1000))
        self.bot = LearningController(self.cfg)
        robot_type = ('red', False)
        for buyer in self.cfg.buyers:
            preferred = robot_type if buyer == 'b1' else ('blue', True)
            self.bot.market.on_sale_result(buyer, preferred, 'sold', 70, None, 10)

        def value():
            state = self.bot._working_state(self.msg())
            context = Ctx(self.cfg, state, self.bot.market, self.bot.chain,
                          self.bot.params, self.bot.wip, self.bot.last_price)
            return context.V[robot_type]

        no_stock = value()
        self.sim.items['robot'] = dict(id='robot', kind='robot',
                                      props=dict(color='red', handles=False))
        ready_stock = value()
        self.assertLess(ready_stock, no_stock)

        self.sim.items.clear()
        self.sim.jobs['m-assemble'] = {'until': 3}
        self.bot.wip['m-assemble'] = ('robot', {'color': 'red', 'handles': False})
        self.assertLess(value(), ready_stock)

    def test_no_restart_until_explicit_machine_release(self):
        self.sim.items['body'] = dict(id='body',kind='body',props=dict(color=None,handles=False))
        self.sim.jobs['m-paint'] = dict(until=5)
        commands = self.bot.handle_event(self.msg(t=5))
        self.assertFalse(any(c.get('machineId') == 'm-paint' for c in commands))

    def test_no_offer_until_explicit_buyer_release(self):
        self.sim.items['robot'] = dict(id='robot',kind='robot',props=dict(color='red',handles=False))
        state = self.sim.state()
        for buyer in state['buyers']:
            buyer['cooldownUntil'] = 10
        msg = EventMsg.from_wire(dict(seq=0,t=10,event=dict(type='idle'),state=state))
        self.assertFalse(any(c['cmd'] == 'offer' for c in self.bot.handle_event(msg)))

    def test_pending_offer_is_matched_by_buyer_and_item(self):
        rt = ('red', False)
        self.bot.pending[('b1', 'robot')] = Pending('b1', rt, 60, 0)
        self.bot.pending[('b2', 'robot')] = Pending('b2', rt, 10, 0)
        event = dict(type='sale_result',buyerId='b1',itemId='robot',
            item=dict(id='robot',kind='robot',props=dict(color='red',handles=False)),
            outcome='refused',reason='below_min_price')
        self.bot._ingest(self.msg(event=event))
        self.assertIn(('b2', 'robot'), self.bot.pending)
        self.assertNotIn(('b1', 'robot'), self.bot.pending)
        self.assertGreater(self.bot.market.interval('b1', rt)[1], 50)

    def test_do_not_reoffer_item_awaiting_another_result(self):
        self.sim.items['robot'] = dict(id='robot',kind='robot',props=dict(color='red',handles=False))
        self.bot.pending[('b1', 'robot')] = Pending('b1', ('red',False), 25, 0)
        self.assertNotIn('robot', self.bot._working_state(self.msg()).items)

    def test_rejected_offer_clears_pending_without_price_learning(self):
        self.bot.pending[('b1', 'robot')] = Pending('b1', ('red',False), 60, 0)
        self.bot.last_commands = [dict(cmd='offer',buyerId='b1',itemId='robot',minPrice=60)]
        self.bot._ingest(self.msg(report=[dict(cmd=0,ok=False,reason='no_such_robot')]))
        self.assertEqual(self.bot.pending, {})
        self.assertEqual(len(self.bot.market.beliefs['b1'].alive()), 6)

    def test_handles_can_be_selected_before_paint_when_more_valuable(self):
        self.sim.items['body'] = dict(id='body',kind='body',props=dict(color=None,handles=False))
        for buyer in self.cfg.buyers:
            self.bot.market.on_sale_result(buyer, ('red',True), 'sold', 70, None, 40)
        commands = self.bot.handle_event(self.msg())
        self.assertTrue(any(c.get('opId') == 'add-handles' for c in commands))
        self.assertFalse(any(c.get('opId', '').startswith('paint') for c in commands))

    def test_no_production_that_finishes_after_shift(self):
        self.sim.items = {i['id']:i for i in [dict(id='body',kind='body',props=dict(color='red',handles=False)),
            dict(id='control',kind='control',props={})]}
        late = self.bot.handle_event(self.msg(t=599))
        self.assertFalse(any(c['cmd'] == 'start' for c in late))
        self.bot = LearningController(self.cfg)
        boundary = self.bot.handle_event(self.msg(t=597))
        self.assertTrue(any(c.get('opId') == 'assemble' for c in boundary))

    def test_tail_uses_durations_from_start(self):
        config = demo_config()
        config['machines'][3]['ops'][0]['duration'] = 7
        self.bot = LearningController(StartMsg.from_wire(config))
        self.sim.items = {i['id']:i for i in [dict(id='body',kind='body',props=dict(color='red',handles=False)),
            dict(id='control',kind='control',props={})]}
        self.assertFalse(any(c['cmd'] == 'start' for c in self.bot.handle_event(self.msg(t=594))))

    def test_late_raw_supplies_are_not_bought(self):
        event = dict(type='supply',item=dict(id='sheet',kind='sheet',props={}),price=4)
        self.assertFalse(any(c['cmd'] == 'accept' for c in self.bot.handle_event(self.msg(t=593,event=event))))

    def test_old_seq_returns_original_answer_and_does_not_relearn(self):
        store = RunStore()
        store.start('a', self.sim.config)
        event = dict(type='supply',item=dict(id='sheet',kind='sheet',props={}),price=4)
        first = dict(seq=0,t=0,event=event,state=self.sim.state())
        response = store.event('a', first)
        for seq in range(1, 40):
            store.event('a', dict(seq=seq,t=seq,event=dict(type='idle'),state=self.sim.state()))
        memory_before = store.get('a').memory['controller'].stats.copy()
        self.assertEqual(store.event('a', first), response)
        self.assertEqual(store.get('a').memory['controller'].stats, memory_before)
        self.assertTrue(response['commands'])

    def test_two_run_models_are_isolated(self):
        store = RunStore()
        for run_id in ('a', 'b'):
            store.start(run_id, self.sim.config)
            store.event(run_id, dict(seq=0,t=0,event=dict(type='idle'),state=self.sim.state()))
        a = store.get('a').memory['controller']
        b = store.get('b').memory['controller']
        a.market.on_sale_result('b1', ('red',False), 'sold', 60, None, 40)
        self.assertEqual(len(a.market.beliefs['b1'].alive()), 1)
        self.assertEqual(len(b.market.beliefs['b1'].alive()), 6)

    def test_supply_schedule_does_not_depend_on_actions(self):
        def supplies(active):
            sim = Simulator(5, 90)
            store = RunStore()
            store.start('test', sim.config)
            timeline = []
            def decide(payload):
                if payload['event']['type'] == 'supply':
                    timeline.append((payload['t'],payload['event']['item']['kind']))
                return store.event('test',payload) if active else dict(commands=[])
            sim.run(decide)
            return timeline
        self.assertEqual(supplies(True), supplies(False))


class DemoRunnerTests(unittest.TestCase):
    def test_only_one_demo_runs_at_a_time_and_shutdown_stops_it(self):
        runner = DemoRunner(RunStore())
        try:
            result = runner.start(dict(horizon=600,delay=.2))
            with self.assertRaises(ApiError) as error:
                runner.start({})
            self.assertEqual(error.exception.code, 'demo_busy')
        finally:
            runner.close()
        self.assertFalse(runner.thread.is_alive())
        self.assertTrue(runner.store.snapshot(result['runId'])['result']['cancelled'])
