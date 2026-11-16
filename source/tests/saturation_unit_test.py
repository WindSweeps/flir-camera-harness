"""Synthetic raw-output saturation checks, independent of Qt/camera hardware."""
import json
import sys
import unittest
import weakref
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import saturation


def metadata(fmt, maximum=None, **nodes):
    result = {'source': 'camera', 'image_info': {'PixelFormat': {'status': 'ok', 'value': fmt}},
              'nodes': {key: {'status': 'ok', 'value': value} for key, value in nodes.items()}}
    if maximum is not None:
        result['nodes']['PixelDynamicRangeMax'] = {'status': 'ok', 'value': maximum}
    return result


class SaturationTests(unittest.TestCase):
    def test_mono8_boundaries_and_roi_only(self):
        raw = np.array([[255, 254], [3, 0]], dtype=np.uint8)
        payload = saturation.prepare_frame(raw, metadata('Mono8'))
        self.assertEqual(payload['threshold'], 255)
        self.assertTrue(saturation.summarize_roi(payload, (0, 0, 1, 1), raw.shape)['saturated'])
        self.assertFalse(saturation.summarize_roi(payload, (1, 0, 2, 2), raw.shape)['saturated'])
        summary = saturation.summarize_roi(payload, (0, 0, 2, 2), raw.shape)
        self.assertEqual((summary['count'], summary['total'], summary['fraction']), (1, 4, .25))
        json.dumps(summary, allow_nan=False)

    def test_raw65520_warns_even_when_display254(self):
        raw = np.array([[65504, 65520]], dtype=np.uint16)
        display = (raw.astype(np.float32) * (255 / 65535)).astype(np.uint8)
        self.assertEqual(int(display[0, 1]), 254)
        payload = saturation.prepare_frame(raw, metadata('Mono16', 65520))
        result = saturation.summarize_roi(payload, (0, 0, 2, 1), display.shape)
        self.assertEqual((result['count'], result['threshold']), (1, 65520))

    def test_documented_model_only_adc_fallback(self):
        raw = np.array([[0, 65520]], dtype=np.uint16)
        source = metadata('Mono16', DeviceModelName='BFS-U3-16S2M-CS', AdcBitDepth='Bit12')
        result = saturation.prepare_frame(raw, source)
        self.assertEqual((result['status'], result['threshold']), ('ok', 65520))
        source['nodes']['DeviceModelName']['value'] = 'FFY-U3-16S2M'
        self.assertEqual(saturation.prepare_frame(raw, source)['status'], 'unknown')
        source['nodes']['DeviceModelName']['value'] = 'BFS-U3-16S2M-CS'
        raw[0, 0] = 1
        self.assertEqual(saturation.prepare_frame(raw, source)['status'], 'unknown')

    def test_explicit_unaligned_range_and_invalid_range(self):
        raw = np.array([[4094, 4095]], dtype=np.uint16)
        self.assertTrue(saturation.prepare_frame(raw, metadata('Mono16', 4095))['mask'][0, 1])
        for maximum in (0, -1, 65536, '4095', 4095.5, True):
            self.assertEqual(saturation.prepare_frame(raw, metadata('Mono16', maximum))['status'], 'unknown')
        raw[0, 1] = 65520
        self.assertEqual(saturation.prepare_frame(raw, metadata('Mono16', 4095))['status'], 'unknown')

    def test_rgb_any_channel_alpha_ignored_unique_pixel_count(self):
        raw = np.array([[[0, 255, 0, 255], [0, 0, 0, 255], [255, 255, 255, 255]]], dtype=np.uint8)
        payload = saturation.prepare_frame(raw, metadata('BGRa8'))
        result = saturation.summarize_roi(payload, (0, 0, 3, 1), raw.shape[:2])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['channel_policy'], 'any_rgb_component_alpha_ignored')

    def test_packed_float_signed_unknown_never_use_display_normalization(self):
        cases = [
            (np.array([[255]], dtype=np.uint8), 'Mono12Packed'),
            (np.array([[4095]], dtype=np.uint16), 'Mono12p'),
            (np.array([[1.]], dtype=np.float32), 'Mono8'),
            (np.array([[127]], dtype=np.int8), 'Mono8'),
            (np.array([[255]], dtype=np.uint8), 'YCbCr8'),
            (np.array([[255]], dtype=np.uint8), None),
            (np.array([[65535]], dtype=np.uint16), 'Mono16'),
        ]
        for raw, fmt in cases:
            payload = saturation.prepare_frame(raw, metadata(fmt))
            self.assertEqual(payload['status'], 'unknown', fmt)
            self.assertIsNone(saturation.summarize_roi(payload, (0, 0, 1, 1), raw.shape)['saturated'])

    def test_frame_metadata_conflicts(self):
        raw = np.array([[255]], dtype=np.uint8)
        self.assertEqual(saturation.prepare_frame(raw, metadata('Mono16'))['status'], 'unknown')
        self.assertEqual(saturation.prepare_frame(raw, metadata('Mono8', 255, PixelFormat='Mono16'))['status'], 'unknown')
        source = metadata('Mono8')
        source['image_info']['Width'] = {'status': 'ok', 'value': 2}
        self.assertEqual(saturation.prepare_frame(raw, source)['status'], 'unknown')

    def test_replay_is_explicit(self):
        payload = saturation.prepare_frame(np.array([[255]], dtype=np.uint8), {}, replay=True)
        self.assertEqual((payload['status'], payload['source']), ('ok', 'replay_uint8_full_scale'))
        self.assertEqual(saturation.prepare_frame(np.array([[65535]], dtype=np.uint16), {}, replay=True)['status'], 'unknown')

    def test_no_roi_mismatched_mask_and_empty_bounds(self):
        raw = np.zeros((2, 2), dtype=np.uint8)
        payload = saturation.prepare_frame(raw, metadata('Mono8'))
        for value in (None, {}, payload):
            self.assertEqual(saturation.summarize_roi(value, None, None)['status'], 'no_roi')
        self.assertEqual(saturation.summarize_roi(payload, (0, 0, 0, 1), raw.shape)['status'], 'no_roi')
        for bounds, shape in (((-1, 0, 1, 1), raw.shape), ((0, 0, 3, 1), raw.shape),
                              ((0, 0, 1, 1), (3, 3)), ((0, 0, 1, 1), None)):
            self.assertEqual(saturation.summarize_roi(payload, bounds, shape)['status'], 'unknown')

    def test_bool_mask_does_not_retain_raw_array_or_copy_it(self):
        class CountedArray(np.ndarray):
            copies = 0
            def copy(self, *args, **kwargs):
                type(self).copies += 1
                return super().copy(*args, **kwargs)
        raw = np.array([[255, 254]], dtype=np.uint8).view(CountedArray)
        reference = weakref.ref(raw)
        payload = saturation.prepare_frame(raw, metadata('Mono8'))
        self.assertEqual(CountedArray.copies, 0)
        self.assertFalse(np.shares_memory(raw, payload['mask']))
        raw[:] = 0
        self.assertTrue(bool(payload['mask'][0, 0]))
        del raw
        self.assertIsNone(reference())

    def test_check_roi_offsets_scalars_and_metadata_unchanged(self):
        raw = np.zeros((4, 6), dtype=np.uint8)
        raw[1, 2] = raw[2, 3] = 255
        source = metadata('Mono8')
        source['image_info'].update(Width={'status': 'ok', 'value': 6},
                                    Height={'status': 'ok', 'value': 4})
        before = json.dumps(source, sort_keys=True)
        result = saturation.check_roi(raw, source, (2, 1, 4, 3))
        self.assertEqual((result['status'], result['count'], result['total'], result['fraction']),
                         ('ok', 2, 4, .5))
        self.assertEqual(result['bounds'], [2, 1, 4, 3])
        self.assertTrue(result['checked'])
        self.assertEqual(result['check_scope'], 'roi_only')
        self.assertNotIn('mask', result)
        json.dumps(result, allow_nan=False)
        self.assertEqual(json.dumps(source, sort_keys=True), before)

    def test_check_roi_never_scans_pixels_outside_selection(self):
        class TrackedArray(np.ndarray):
            scanned_shapes = []
            def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
                type(self).scanned_shapes.append(tuple(self.shape))
                plain = tuple(np.asarray(item) if isinstance(item, TrackedArray) else item
                              for item in inputs)
                return getattr(ufunc, method)(*plain, **kwargs)
        raw = np.zeros((40, 60), dtype=np.uint16).view(TrackedArray)
        raw[0, 0] = 65535  # Deliberately conflicts with the stated upper bound.
        raw[20, 30] = 65520
        reference = weakref.ref(raw)
        result = saturation.check_roi(raw, metadata('Mono16', 65520), (30, 20, 32, 23))
        self.assertEqual((result['status'], result['count'], result['total']), ('ok', 1, 6))
        self.assertTrue(TrackedArray.scanned_shapes)
        self.assertEqual(set(TrackedArray.scanned_shapes), {(3, 2)})
        del raw
        self.assertIsNone(reference())
        json.dumps(result, allow_nan=False)

    def test_check_roi_rejects_full_frame_metadata_mismatch_and_bad_bounds(self):
        raw = np.zeros((4, 6), dtype=np.uint8)
        source = metadata('Mono8')
        source['image_info']['Width'] = {'status': 'ok', 'value': 2}
        self.assertEqual(saturation.check_roi(raw, source, (2, 1, 4, 3))['status'], 'unknown')
        for bounds in ((-1, 0, 2, 2), (0, 0, 7, 1), (0, 0, 1, 5), (0, 0, 1.5, 1), 'oops'):
            result = saturation.check_roi(raw, metadata('Mono8'), bounds)
            self.assertEqual(result['status'], 'unknown')
            self.assertIsNone(result['saturated'])
            self.assertTrue(result['checked'])
        self.assertEqual(saturation.check_roi(raw, {}, None)['status'], 'no_roi')
        self.assertEqual(saturation.check_roi(raw, {}, (1, 1, 1, 2))['status'], 'no_roi')

    def test_check_roi_threshold_unknown_replay_and_color(self):
        unknown = saturation.check_roi(np.array([[1.]], dtype=np.float32), {}, (0, 0, 1, 1))
        self.assertEqual(unknown['status'], 'unknown')
        self.assertIsNone(unknown['saturated'])
        replay = saturation.check_roi(np.array([[255]], dtype=np.uint8), {}, (0, 0, 1, 1), replay=True)
        self.assertTrue(replay['saturated'])
        self.assertEqual(replay['source'], 'replay_uint8_full_scale')
        color = np.zeros((3, 4, 4), dtype=np.uint8)
        color[:, :, 3] = 255
        color[1, 2, 1] = 255
        result = saturation.check_roi(color, metadata('BGRa8'), (2, 1, 4, 3))
        self.assertEqual((result['count'], result['total']), (1, 4))


if __name__ == '__main__':
    unittest.main(verbosity=2)
