"""Hardware-free protocol invariants; no Qt/runtime dependency."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from harness_core import Protocol, TOOLS, ToolError, VERSIONS


class Adapter:
    def __init__(self): self.calls = []
    def call(self, name, args):
        self.calls.append((name, args))
        if name == 'export_curve':
            raise ToolError('SATURATION_WARNING', 'User agreement needed')
        return {'value': args}


class Tests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.server = Protocol(self.adapter)
    def request(self, method, params=None, **extra):
        return self.server.handle({'jsonrpc':'2.0','id':1,'method':method,'params':params or {},**extra})
    def init(self, version=VERSIONS[0]):
        result = self.request('initialize', {'protocolVersion':version,'capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        self.server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        return result
    def test_lifecycle_and_fallback(self):
        self.assertEqual(self.request('server/discover')['error']['code'], -32601)
        self.assertIn('error', self.request('tools/list'))
        self.assertEqual(self.init('2030-01-01')['result']['protocolVersion'], VERSIONS[0])
        self.assertEqual(len(self.request('tools/list')['result']['tools']), len(TOOLS))
        self.assertIn('error', self.request('initialize'))
    def test_invalid_calls_do_not_mutate(self):
        self.init()
        for args in ({'exposure_us': True}, {'exposure_us':float('nan')}, {'exposure_us':0}, {'exposure_us':20,'arbitrary':1}, {}):
            r = self.request('tools/call', {'name':'flir_set_exposure','arguments':args})
            self.assertTrue(r['result']['isError'], args)
        self.assertEqual(self.adapter.calls, [])
    def test_notifications_never_mutate(self):
        self.init()
        self.assertIsNone(self.server.handle({'jsonrpc':'2.0','method':'tools/call','params':{'name':'flir_start_capture'}}))
        self.assertEqual(self.adapter.calls, [])
    def test_error_and_legacy_content(self):
        for version in VERSIONS:
            self.setUp()
            self.init(version)
            r = self.request('tools/call', {'name':'flir_export_curve','arguments':{'path':'curve.csv'}})['result']
            self.assertTrue(r['isError'])
            self.assertEqual('structuredContent' in r, version in VERSIONS[:2])
    def test_nested_context_validation(self):
        self.init()
        for values in ({'nd_transmission_fraction':2}, {'wavelength_nm':0}, {'calibration_status':'calibrated'}):
            self.assertTrue(self.request('tools/call',{'name':'flir_context_set','arguments':{'values':values}})['result']['isError'])
        self.assertEqual(self.adapter.calls, [])
    def test_ids_and_unknown_methods(self):
        for rid in (None, True, [], 1.5):
            self.assertIn('error', self.request('ping', id=rid))
        self.assertEqual(self.request('run_shell')['error']['code'], -32601)
        self.assertIn('error', self.server.handle([]))


if __name__ == '__main__': unittest.main()
