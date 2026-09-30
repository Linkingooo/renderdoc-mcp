"""Synthetic timing regressions; import neither MCP SDK nor native RenderDoc."""
import importlib.util
import json
from enum import IntEnum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class GPUCounter(IntEnum):
    EventGPUDuration = 1


class CounterUnit(IntEnum):
    Seconds = 1
    Cycles = 2


class CompType(IntEnum):
    Float = 1
    UInt = 2
    Double = 3
    SInt = 4


class ActionFlags(IntEnum):
    Drawcall = 1
    Clear = 2
    Dispatch = 4


class ToolCollector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate


def load_subject():
    """Stub imports before evaluating the upstream module: no DLL loader runs."""
    names = ['mcp', 'mcp.server', 'mcp.server.fastmcp', 'renderdoc_mcp',
             'renderdoc_mcp.session', 'renderdoc_mcp.util']
    modules = {name: ModuleType(name) for name in names}
    for name in ('mcp', 'mcp.server', 'renderdoc_mcp'):
        modules[name].__path__ = []
    modules['mcp.server.fastmcp'].FastMCP = ToolCollector
    modules['renderdoc_mcp.session'].get_session = lambda: None
    util = modules['renderdoc_mcp.util']
    util.rd = SimpleNamespace(GPUCounter=GPUCounter, CounterUnit=CounterUnit,
                              CompType=CompType, ActionFlags=ActionFlags)
    util.to_json = lambda data: json.dumps(data, allow_nan=False)
    util.make_error = lambda message, code: {'error': message, 'code': code}
    for name in ('flags_to_list', 'SHADER_STAGE_MAP', 'BLEND_FACTOR_MAP',
                 'COMPARE_FUNC_MAP', 'enum_str'):
        setattr(util, name, {})
    filename = Path(__file__).resolve().parents[1] / 'src/renderdoc_mcp/tools/performance_tools.py'
    spec = importlib.util.spec_from_file_location('timing_test_subject', filename)
    subject = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(subject)
    return subject


SUBJECT = load_subject()


class UnionValue:
    """Fail tests if the implementation reads the wrong native union member."""
    def __init__(self, member, value):
        self.member, self.value = member, value

    def __getattr__(self, member):
        if member != self.member:
            raise AssertionError('Wrong CounterValue member: ' + member)
        return self.value


def row(event, value, member='d', counter=GPUCounter.EventGPUDuration):
    return SimpleNamespace(eventId=event, counter=counter, value=UnionValue(member, value))


def action(event, outputs=(5,), flags=ActionFlags.Drawcall, indices=900000):
    return SimpleNamespace(flags=flags, outputs=outputs, numIndices=indices,
                           GetName=lambda sf: 'draw-' + str(event))


class Controller:
    def __init__(self, results=(), counters=(GPUCounter.EventGPUDuration,),
                 kind=CompType.Float, width=8, unit=CounterUnit.Seconds):
        self.results, self.counters = list(results), list(counters)
        self.description = SimpleNamespace(name='GPU duration', resultType=kind,
                                           resultByteWidth=width, unit=unit)
        self.calls = []
        self.failure = None

    def EnumerateCounters(self):
        if self.failure == 'enumerate': raise RuntimeError('synthetic failure')
        return self.counters

    def DescribeCounter(self, counter):
        self.calls.append(('describe', counter))
        if self.failure == 'describe': raise RuntimeError('synthetic failure')
        return self.description

    def FetchCounters(self, counters):
        self.calls.append(('fetch', counters))
        if self.failure == 'fetch': raise RuntimeError('synthetic failure')
        return self.results


class TimingTests(unittest.TestCase):
    def invoke(self, controller, actions=None, granularity='draw_call', top_n=20):
        session = SimpleNamespace(controller=controller, structured_file=None,
                                  action_map={10: action(10)} if actions is None else actions,
                                  require_open=lambda: None, get_texture_desc=lambda resource: None)
        collector = ToolCollector()
        SUBJECT.register(collector)
        with patch.object(SUBJECT, 'get_session', return_value=session):
            return json.loads(collector.tools['get_pass_timing'](granularity, top_n))

    def test_seconds_converted_to_ms_and_metadata_retained(self):
        data = self.invoke(Controller([row(10, 0.0025)]))
        entry = data['entries'][0]
        self.assertEqual(entry['estimated_cost'], 2.5)
        self.assertEqual(entry['gpu_duration_ms'], 2.5)
        self.assertEqual(entry['timing_status'], 'valid')
        self.assertEqual(data['counter']['unit'], 'Seconds')
        self.assertEqual(data['counter']['result_byte_width'], 8)
        self.assertEqual(data['timing_unit'], 'ms')
        self.assertEqual(data['timing_source'], 'gpu_counter_replay')

    def test_float32_reads_f_not_double(self):
        data = self.invoke(Controller([row(10, 0.004, 'f')], width=4))
        self.assertEqual(data['entries'][0]['estimated_cost'], 4.0)
        self.assertEqual(data['entries'][0]['gpu_duration_ms'], 4.0)

    def test_unsigned_widths_read_u32_and_u64(self):
        for width, member in ((4, 'u32'), (8, 'u64')):
            with self.subTest(width=width):
                data = self.invoke(Controller([row(10, 2, member)], kind=CompType.UInt, width=width))
                self.assertEqual(data['entries'][0]['gpu_duration_ms'], 2000.0)

    def test_explicit_double_reads_d(self):
        data = self.invoke(Controller([row(10, 0.003)], kind=CompType.Double))
        self.assertEqual(data['entries'][0]['gpu_duration_ms'], 3.0)

    def test_valid_zero_is_available_and_complete(self):
        data = self.invoke(Controller([row(10, 0.0)]))
        self.assertTrue(data['has_real_timing'])
        self.assertEqual(data['entries'][0]['gpu_duration_ms'], 0.0)
        self.assertEqual(data['timing_status'], 'complete')
        self.assertTrue(data['timing_coverage']['complete'])

    def test_small_positive_duration_is_not_rounded_to_zero(self):
        data = self.invoke(Controller([row(10, 1e-9)]))
        self.assertGreater(data['entries'][0]['gpu_duration_ms'], 0)

    def test_builtin_counter_selected_instead_of_first_named_time(self):
        controller = Controller([row(10, 0.001)], counters=(9001, GPUCounter.EventGPUDuration))
        data = self.invoke(controller)
        self.assertEqual(controller.calls, [('describe', GPUCounter.EventGPUDuration),
                                            ('fetch', [GPUCounter.EventGPUDuration])])
        self.assertEqual(data['counter']['id'], int(GPUCounter.EventGPUDuration))

    def test_no_builtin_counter_never_uses_triangle_cost(self):
        data = self.invoke(Controller(counters=(9001,)))
        self.assertFalse(data['has_real_timing'])
        self.assertIsNone(data['timing_unit'])
        self.assertIsNone(data['entries'][0]['estimated_cost'])
        self.assertEqual(data['entries'][0]['timing_reason'], 'gpu_duration_counter_unavailable')

    def test_empty_results_are_missing_not_zero(self):
        data = self.invoke(Controller())
        self.assertEqual(data['timing_status'], 'unavailable')
        self.assertFalse(data['has_real_timing'])
        self.assertIsNone(data['entries'][0]['gpu_duration_ms'])
        self.assertEqual(data['entries'][0]['timing_reason'], 'event_result_missing')

    def test_partial_coverage_survives_top_n(self):
        actions = {eid: action(eid) for eid in (10, 20, 30)}
        data = self.invoke(Controller([row(10, 0.0), row(20, 0.002)]), actions, top_n=1)
        self.assertEqual(len(data['entries']), 1)
        self.assertEqual(data['timing_status'], 'partial')
        self.assertEqual(data['timing_coverage'], {'total_draws': 3, 'valid_draws': 2,
                                                 'missing_draws': 1, 'invalid_draws': 0, 'complete': False})

    def test_missing_draw_has_per_event_reason(self):
        data = self.invoke(Controller([row(10, 0.001)]), {10: action(10), 20: action(20)})
        missing = next(item for item in data['entries'] if item['event_id'] == 20)
        self.assertIsNone(missing['estimated_cost'])
        self.assertEqual(missing['timing_status'], 'missing')
        self.assertEqual(missing['timing_reason'], 'event_result_missing')

    def test_negative_nonfinite_and_overflow_values_are_invalid(self):
        for value in (-0.1, float('nan'), float('inf'), 1e308):
            with self.subTest(value=value):
                data = self.invoke(Controller([row(10, value)]))
                self.assertEqual(data['entries'][0]['timing_status'], 'invalid')
                self.assertIsNone(data['entries'][0]['gpu_duration_ms'])
                self.assertEqual(data['timing_coverage']['invalid_draws'], 1)

    def test_unsigned_out_of_range_is_invalid(self):
        for value in (-1, 2 ** 32, True):
            with self.subTest(value=value):
                data = self.invoke(Controller([row(10, value, 'u32')], kind=CompType.UInt, width=4))
                self.assertEqual(data['entries'][0]['timing_status'], 'invalid')

    def test_unsupported_unit_does_not_fetch_or_convert(self):
        controller = Controller([row(10, 4)], unit=CounterUnit.Cycles)
        data = self.invoke(controller)
        self.assertEqual(data['counter']['unit'], 'Cycles')
        self.assertIsNone(data['timing_unit'])
        self.assertIsNone(data['entries'][0]['gpu_duration_ms'])
        self.assertEqual(data['entries'][0]['timing_reason'], 'unsupported_counter_unit')
        self.assertEqual(len(controller.calls), 1)

    def test_unsupported_type_or_width_has_reason(self):
        for kind, width in ((CompType.SInt, 8), (CompType.Float, 2), (CompType.Double, 4)):
            with self.subTest(kind=kind, width=width):
                data = self.invoke(Controller(kind=kind, width=width))
                self.assertEqual(data['entries'][0]['timing_reason'], 'unsupported_counter_type_or_width')

    def test_api_failures_do_not_become_zero(self):
        reasons = {'enumerate': 'counter_enumeration_failed', 'describe': 'counter_description_failed',
                   'fetch': 'counter_fetch_failed'}
        for failure, reason in reasons.items():
            with self.subTest(failure=failure):
                controller = Controller(); controller.failure = failure
                data = self.invoke(controller)
                self.assertIsNone(data['entries'][0]['gpu_duration_ms'])
                self.assertEqual(data['entries'][0]['timing_reason'], reason + ':RuntimeError')

    def test_duplicate_results_are_invalid(self):
        data = self.invoke(Controller([row(10, 0.001), row(10, 0.002)]))
        self.assertEqual(data['entries'][0]['timing_reason'], 'duplicate_counter_result')
        self.assertIsNone(data['entries'][0]['gpu_duration_ms'])

    def test_unrequested_counter_results_are_ignored(self):
        data = self.invoke(Controller([row(10, 0.009, counter=9001)]))
        self.assertEqual(data['entries'][0]['timing_status'], 'missing')

    def test_pass_partial_sum_and_coverage_exclude_clear_counters(self):
        actions = {1: action(1, flags=ActionFlags.Clear), 10: action(10), 20: action(20)}
        data = self.invoke(Controller([row(1, 10.0), row(10, 0.003)]), actions, granularity='pass')
        group = data['passes'][0]
        self.assertEqual(group['gpu_duration_ms'], 3.0)
        self.assertEqual(group['timing_status'], 'partial')
        self.assertEqual(group['timing_reason'], 'incomplete_counter_coverage')
        self.assertEqual(group['timing_reasons'], {'event_result_missing': 1})
        self.assertEqual(group['timing_coverage']['total_draws'], 2)
        self.assertEqual(group['timing_coverage']['missing_draws'], 1)
        self.assertEqual(data['grouping_source'], 'clear_and_output_target_heuristic')

    def test_pass_missing_and_zero_groups_are_distinct(self):
        actions = {10: action(10, (5,)), 20: action(20, (6,))}
        data = self.invoke(Controller([row(10, 0.0)]), actions, granularity='pass')
        groups = {item['start_event']: item for item in data['passes']}
        self.assertEqual(groups[10]['gpu_duration_ms'], 0.0)
        self.assertEqual(groups[10]['timing_status'], 'complete')
        self.assertIsNone(groups[20]['gpu_duration_ms'])
        self.assertEqual(groups[20]['timing_status'], 'unavailable')

    def test_complete_groups_rank_before_partial_subtotals(self):
        actions = {10: action(10, (5,)), 20: action(20, (6,)), 30: action(30, (6,))}
        data = self.invoke(Controller([row(10, 0.001), row(20, 0.1)]), actions, granularity='pass')
        self.assertEqual(data['passes'][0]['start_event'], 10)
        self.assertEqual(data['passes'][1]['timing_status'], 'partial')

    def test_group_aggregate_overflow_is_not_serialized_as_infinity(self):
        data = self.invoke(Controller([row(10, 1e305), row(20, 1e305)]),
                           {10: action(10), 20: action(20)}, granularity='pass')
        self.assertIsNone(data['passes'][0]['gpu_duration_ms'])
        self.assertEqual(data['passes'][0]['timing_status'], 'invalid')
        self.assertEqual(data['passes'][0]['timing_reason'], 'aggregate_duration_out_of_range')

    def test_clear_only_group_is_not_reported_as_zero_time(self):
        data = self.invoke(Controller([row(1, 0.0)]), {1: action(1, flags=ActionFlags.Clear)}, granularity='pass')
        self.assertEqual(data['passes'][0]['timing_status'], 'no_draws')
        self.assertIsNone(data['passes'][0]['gpu_duration_ms'])
        self.assertFalse(data['has_real_timing'])

    def test_invalid_granularity_returns_error(self):
        data = self.invoke(Controller(), granularity='unsupported')
        self.assertEqual(data['code'], 'API_ERROR')

    def test_missing_counter_description_field_is_unavailable(self):
        controller = Controller()
        controller.description = SimpleNamespace(name='GPU duration')
        data = self.invoke(controller)
        self.assertIsNone(data['entries'][0]['estimated_cost'])
        self.assertEqual(data['entries'][0]['timing_reason'], 'counter_description_failed:AttributeError')

    def test_empty_draw_scope_has_no_zero_total_or_complete_coverage(self):
        data = self.invoke(Controller(), actions={}, granularity='pass')
        self.assertEqual(data['timing_status'], 'no_draws')
        self.assertEqual(data['passes'], [])
        self.assertFalse(data['timing_coverage']['complete'])
        self.assertEqual(data['timing_coverage']['total_draws'], 0)


if __name__ == '__main__':
    unittest.main()
