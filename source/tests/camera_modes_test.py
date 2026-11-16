"""Hardware-free transactions: selector scopes, restart, rollback, auto gain."""
import copy
import json
import camera_modes as modes
import capture_metadata as metadata


class Entry:
    def __init__(self, value, available=True):
        self.value, self.available, self.readable = value, available, True
    def GetValue(self): return self.value
    def GetSymbolic(self): return self.value


class Node:
    available = readable = writable = True
    def __init__(self, camera, name, kind, value, choices=(), scope=None, low=0, high=100000000):
        self.camera, self.name, self.kind = camera, name, kind
        self.value, self.choices, self.scope = value, choices, scope
        self.low, self.high, self.fail, self.mismatch = low, high, None, None
    def GetPrincipalInterfaceType(self): return self.kind
    def GetValue(self, *unused):
        return self.value[self.camera.nodes[self.scope].GetValue()] if self.scope else self.value
    def GetCurrentEntry(self, *unused): return Entry(self.GetValue())
    def GetEntryByName(self, name): return Entry(name, name in self.choices)
    def GetMin(self): return self.low
    def GetMax(self): return self.high
    def GetUnit(self): return ''
    def SetIntValue(self, value): self.SetValue(value)
    def SetValue(self, value):
        assert not self.camera.backend.streaming, 'node writes must happen while stopped'
        self.camera.trace.append((self.name, value))
        if self.fail == value:
            raise RuntimeError('injected write failure ' + self.name)
        actual = self.mismatch if self.mismatch is not None else value
        if self.scope:
            self.value[self.camera.nodes[self.scope].GetValue()] = actual
        else:
            self.value = actual


class Spin:
    intfIFloat, intfIInteger, intfIBoolean, intfIEnumeration, intfIString = range(5)
    IsAvailable = staticmethod(lambda n: n is not None and n.available)
    IsReadable = staticmethod(lambda n: n is not None and n.readable)
    IsWritable = staticmethod(lambda n: n is not None and n.writable)
    CFloatPtr = CIntegerPtr = CBooleanPtr = CEnumerationPtr = CStringPtr = staticmethod(lambda n: n)


class Camera:
    def __init__(self, backend):
        self.backend, self.trace, self.nodes = backend, [], {}
        self.end_error = False
        def add(name, kind, value, choices=(), scope=None, **kw):
            self.nodes[name] = Node(self, name, kind, value, choices, scope, **kw)
        E, F, B = Spin.intfIEnumeration, Spin.intfIFloat, Spin.intfIBoolean
        add('TriggerSelector', E, 'FrameBurstStart', ('FrameStart', 'AcquisitionStart', 'FrameBurstStart'))
        add('TriggerMode', E, {'FrameStart': 'Off', 'AcquisitionStart': 'On', 'FrameBurstStart': 'On'}, ('Off', 'On'), 'TriggerSelector')
        add('AcquisitionMode', E, 'SingleFrame', ('SingleFrame', 'Continuous'))
        add('ExposureMode', E, 'Timed', ('Timed', 'TriggerWidth'))
        add('LineSelector', E, 'Line1', ('Line0', 'Line1'))
        add('LineMode', E, {'Line0': 'Input', 'Line1': 'Output'}, ('Input', 'Output'), 'LineSelector')
        add('LineInverter', B, {'Line0': True, 'Line1': False}, scope='LineSelector')
        add('TriggerSource', E, 'Software', ('Software', 'Line0'))
        add('TriggerActivation', E, 'FallingEdge', ('FallingEdge', 'RisingEdge'))
        add('TriggerOverlap', E, 'ReadOut', ('ReadOut', 'Off'))
        add('TriggerDelay', F, 0.0)
        add('GainAuto', E, 'Off', ('Off', 'Continuous'))
        add('ExposureAuto', E, 'Off', ('Off', 'Continuous'))
        add('Gain', F, 1.0, high=24)
        add('ExposureTime', F, 3000.0)
        add('AutoExposureGainLowerLimit', F, 0.0, high=24)
        add('AutoExposureGainUpperLimit', F, 18.0, high=24)
        add('StreamBufferHandlingMode', E, 'NewestOnly', ('NewestOnly', 'OldestFirst'))
    def GetNodeMap(self): return self
    def GetTLDeviceNodeMap(self): return self
    def GetTLStreamNodeMap(self): return self
    def GetNode(self, name): return self.nodes.get(name)
    def EndAcquisition(self):
        self.trace.append(('EndAcquisition', None))
        if self.end_error: raise RuntimeError('injected stop failure')


class MockBackend:
    def __init__(self, serial='MOCK'):
        self.serial, self.streaming, self._spin = serial, False, Spin
        self.camera = Camera(self)
        self.start_failures = 0
    def start(self):
        self.camera.trace.append(('BeginAcquisition', None))
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError('injected start failure')
        self.streaming = True
    def stop(self):
        self.camera.EndAcquisition()
        self.streaming = False
    def close(self): self.streaming = False
    def set_exposure_us(self, value):
        self.camera.nodes['ExposureTime'].value = value
        return value
    def set_gain_db(self, value):
        self.camera.nodes['Gain'].value = value
        return value
    def set_exposure_auto(self, enabled): raise AssertionError('obsolete auto setter')
    def get_gain_db(self): return modes.read(self, 'Gain')
    def _get_float(self, name): return modes.read(self, name)
    def exposure_range(self): return (1, 1000000, 1)
    def gain_range(self): return (0, 24, .1)
    def grab_frame(self, timeout_ms=5): return None


modes.install(MockBackend)


def snapshot(backend):
    return {name: copy.deepcopy(node.value) for name, node in backend.camera.nodes.items()}


def run_camera_mode_tests():
    cases = []
    backend = MockBackend()
    backend.streaming = True
    backend.camera.nodes['LineMode'].writable = False  # fixed Line0 input is valid
    result = backend.set_trigger_mode('external', 9)
    assert backend.streaming and result['readback']['TriggerMode'] == 'On'
    assert result['readback']['LineInverter'] is False
    assert result['readback']['TriggerActivation'] == 'RisingEdge'
    assert result['readback']['TriggerDelay'] == 9
    trace = backend.camera.trace
    assert trace[0][0] == 'EndAcquisition' and trace[-1][0] == 'BeginAcquisition'
    assert trace[-2] == ('TriggerMode', 'On')
    assert not any(name in ('LineSource', 'V3_3Enable', 'UserOutputValue') for name, value in trace)
    assert backend.camera.nodes['TriggerMode'].value == {'FrameStart':'On', 'AcquisitionStart':'Off', 'FrameBurstStart':'Off'}
    cases.append('external exact readback, fixed input, enable last, stop/resume, no output writes')
    backend.set_trigger_mode('continuous')
    assert backend.streaming and all(value == 'Off' for value in backend.camera.nodes['TriggerMode'].value.values())
    cases.append('continuous disables all supported trigger gates')
    for failure in ('unsupported', 'write', 'read_only', 'restart'):
        b = MockBackend()
        b.streaming = True
        if failure == 'unsupported': b.camera.nodes['TriggerSource'].choices = ('Software',)
        if failure == 'write': b.camera.nodes['TriggerActivation'].fail = 'RisingEdge'
        if failure == 'read_only': b.camera.nodes['LineInverter'].writable = False
        if failure == 'restart': b.start_failures = 1
        before = snapshot(b)
        try: b.set_trigger_mode('external')
        except modes.ConfigurationError: pass
        else: raise AssertionError('failure must surface: ' + failure)
        assert snapshot(b) == before and b.streaming, failure
        cases.append('rollback selector scopes and resume: ' + failure)
    b = MockBackend()
    b.streaming = True
    b.camera.nodes['TriggerDelay'].mismatch = 5
    try: b.set_trigger_mode('external', 9)
    except modes.ConfigurationError as exc: assert '恢复未完成' in str(exc)
    else: raise AssertionError('mismatch must fail')
    assert not b.streaming
    cases.append('readback mismatch and failed rollback stop acquisition')
    b = MockBackend()
    b.streaming = True
    b.camera.end_error = True
    try: b.set_trigger_mode('external')
    except modes.ConfigurationError: pass
    else: raise AssertionError('stop error must surface')
    assert b.camera.trace == [('EndAcquisition', None)] and not b.streaming
    cases.append('stop failure prevents node writes')
    b = MockBackend()
    b.streaming = True
    b._auto_gain_upper_requested = 12
    b.set_exposure_auto(True)
    assert modes.read(b, 'ExposureAuto') == modes.read(b, 'GainAuto') == 'Continuous'
    assert modes.read(b, 'AutoExposureGainUpperLimit') == 12 and b.streaming
    b.set_exposure_auto(False)
    assert modes.read(b, 'ExposureAuto') == modes.read(b, 'GainAuto') == 'Off'
    cases.append('auto enables and disables exposure plus gain, bounded gain')
    for method, value in (('set_exposure_us', 1234), ('set_gain_db', 4)):
        b.set_exposure_auto(True)
        assert getattr(b, method)(value) == value
        assert modes.read(b, 'GainAuto') == modes.read(b, 'ExposureAuto') == 'Off'
    cases.append('manual exposure and gain leave both automatic modes off')
    b.set_exposure_auto(True)
    before = snapshot(b)
    b._auto_gain_upper_requested = 99
    try: b.set_exposure_auto(True)
    except modes.ConfigurationError: pass
    else: raise AssertionError('out of range must fail')
    assert snapshot(b) == before and b.streaming
    cases.append('invalid gain cap restores previous auto state')
    events = metadata.get_parameter_events(0, backend=backend)
    assert len(events) == 2 and events[-1]['action'] == 'set_trigger_mode'
    assert events[0]['result']['readback']['TriggerDelay'] == 9
    cases.append('configuration changes log requested and actual settings')
    b = MockBackend()
    b.streaming = True
    b._photo_autosave_enabled = True
    before = snapshot(b)
    b.camera.nodes['TriggerActivation'].fail = 'RisingEdge'
    try: b.set_trigger_mode('external')
    except modes.ConfigurationError: pass
    else: raise AssertionError('trigger failure must roll back photo buffer mode too')
    assert snapshot(b) == before and b.streaming
    b.camera.nodes['TriggerActivation'].fail = None
    b.set_trigger_mode('external')
    assert modes.read(b, 'StreamBufferHandlingMode') == 'OldestFirst'
    b.set_trigger_mode('continuous')
    assert modes.read(b, 'StreamBufferHandlingMode') == 'NewestOnly'
    cases.append('photo FIFO buffer mode switches and rolls back with trigger transaction')
    print(json.dumps({'status':'passed', 'cases':cases, 'hardware_access':False}))


if __name__ == '__main__':
    run_camera_mode_tests()
