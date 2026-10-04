"""Offline observability regression tests; no model, ffmpeg or network activity."""
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
kick = types.ModuleType('kick_api')
kick.load_config = Mock(return_value={})
burn = types.ModuleType('burn_request')
burn.KICK_URL_RE = burn.TWITCH_URL_RE = Mock()
chapters = types.ModuleType('chapters')
chapters.GEMINI_URL = 'never-used'
chapters.MODEL_FALLBACKS = ['never-used']
preflight = types.ModuleType('transcription_preflight')
preflight.check_input_decode = Mock()
STUBS = {'kick_api': kick, 'burn_request': burn, 'chapters': chapters,
         'transcription_preflight': preflight}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = Path(__file__).with_name('shorts_prep.py')
if not SOURCE.is_file():
    SOURCE = ROOT / 'candidate/shorts_prep.py'
ORIGINAL = ROOT / 'original/shorts_prep.py'
with patch.dict(sys.modules, STUBS):
    old = load('original_shorts', ORIGINAL) if ORIGINAL.is_file() else None
    new = load('candidate_shorts', SOURCE)


def segment(text='speech', start=0.25, end=2.5):
    return types.SimpleNamespace(text=text, start=start, end=end)


def info(duration=10.0, after=8.0):
    return types.SimpleNamespace(duration=duration, duration_after_vad=after)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        modules = patch.dict(sys.modules, STUBS)
        modules.start()
        self.addCleanup(modules.stop)
        for name in ('subprocess.run', 'urllib.request.urlopen', 'pathlib.Path.unlink', 'pathlib.Path.mkdir'):
            value = Mock(side_effect=AssertionError('network forbidden')) if name == 'urllib.request.urlopen' else Mock()
            p = patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        preflight.check_input_decode.reset_mock()

    def transcribe(self, module, rows, metadata, diagnostics=None):
        model = Mock()
        model.transcribe.return_value = (iter(rows), metadata)
        writes = []
        with patch.object(Path, 'write_text', side_effect=lambda text, **kwargs: writes.append(text)):
            args = {} if diagnostics is None else {'diagnostics': diagnostics}
            result = module.transcribe_srt(model, 'clip.mp4', 'clip.srt', **args)
        self.assertEqual(len(writes), 1)
        model.transcribe.assert_called_once_with('clip_audio.wav', language='ja', beam_size=1,
                                                vad_filter=True, vad_parameters={'min_silence_duration_ms': 700})
        return result, writes[0]

    def test_original_srt_and_return_are_identical(self):
        rows = [segment(' first '), segment(''), segment('second', 3.75, 5.25)]
        before = self.transcribe(old, rows, info()) if old else (
            ['first', 'second'],
            '1\n00:00:00,250 --> 00:00:02,500\nfirst\n\n2\n00:00:03,750 --> 00:00:05,250\nsecond\n')
        diagnostic = {}
        after = self.transcribe(new, rows, info(), diagnostic)
        self.assertEqual(after, before)
        self.assertEqual(after[0], ['first', 'second'])

    def test_optional_diagnostics_preserves_three_argument_call(self):
        self.assertEqual(self.transcribe(new, [segment()], info())[0], ['speech'])

    def test_empty_srt_remains_an_artifact(self):
        self.assertEqual(self.transcribe(new, [], info(after=0), {})[:2], ([], ''))

    def test_normal_subtitles_have_transcribed_status_and_counts(self):
        diagnostic = {}
        self.transcribe(new, [segment(), segment('   '), segment('next', 4, 6)], info(), diagnostic)
        self.assertEqual(diagnostic['status'], 'transcribed')
        self.assertIsNone(diagnostic['cause'])
        self.assertEqual((diagnostic['raw_segments'], diagnostic['nonblank_subtitles'], diagnostic['blank_segments']),
                         (3, 2, 1))
        self.assertEqual(diagnostic['duration_info_status'], 'known')
        self.assertFalse(diagnostic['vad_no_speech_detected'])

    def test_normal_result_logs_duration_and_counts_when_diagnostics_requested(self):
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.transcribe(new, [segment()], info(), {})
        self.assertIn('transcribed', log.getvalue())
        for token in ('raw_segments=1', 'nonblank_subtitles=1', 'blank_segments=0',
                      'duration=10.0', 'duration_after_vad=8.0'):
            self.assertIn(token, log.getvalue())
        self.assertEqual(len(log.getvalue().splitlines()), 1)

    def test_three_argument_legacy_call_does_not_add_diagnostic_log(self):
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.transcribe(new, [segment()], info())
        self.assertEqual(log.getvalue(), '')

    def test_vad_zero_still_requires_audio_review_and_cause_is_undetermined(self):
        diagnostic = {}
        self.transcribe(new, [], info(after=0), diagnostic)
        self.assertEqual(diagnostic['status'], 'needs-audio-review')
        self.assertEqual(diagnostic['cause'], 'undetermined')
        self.assertTrue(diagnostic['vad_no_speech_detected'])
        self.assertEqual(diagnostic['duration_after_vad'], 0)

    def test_vad_kept_audio_but_zero_text_is_not_classified_as_silent(self):
        diagnostic = {}
        self.transcribe(new, [], info(after=8), diagnostic)
        self.assertEqual(diagnostic['status'], 'needs-audio-review')
        self.assertEqual(diagnostic['cause'], 'undetermined')
        self.assertFalse(diagnostic['vad_no_speech_detected'])

    def test_blank_raw_segments_are_distinguished_from_no_raw_segments(self):
        diagnostic = {}
        result, srt = self.transcribe(new, [segment(''), segment(' \t\n')], info(after=8), diagnostic)
        self.assertEqual((result, srt), ([], ''))
        self.assertEqual((diagnostic['raw_segments'], diagnostic['blank_segments'], diagnostic['nonblank_subtitles']),
                         (2, 2, 0))
        self.assertEqual(diagnostic['cause'], 'undetermined')

    def test_missing_info_is_explicitly_unknown_and_json_safe(self):
        for metadata in (None, object(), types.SimpleNamespace(duration=10)):
            with self.subTest(metadata=metadata):
                diagnostic = {}
                self.transcribe(new, [], metadata, diagnostic)
                self.assertEqual(diagnostic['duration_info_status'], 'unknown')
                self.assertIsNone(diagnostic['duration_after_vad'])
                self.assertIsNone(diagnostic['vad_no_speech_detected'])
                json.dumps(diagnostic, allow_nan=False)

    def test_invalid_duration_values_become_unknown_without_changing_srt(self):
        for value in (float('nan'), float('inf'), -float('inf'), -1, True, '10'):
            with self.subTest(value=value):
                diagnostic = {}
                result, srt = self.transcribe(new, [segment()], info(value, value), diagnostic)
                self.assertEqual(result, ['speech'])
                self.assertTrue(srt)
                self.assertIsNone(diagnostic['duration'])
                self.assertIsNone(diagnostic['duration_after_vad'])
                self.assertIsNone(diagnostic['vad_no_speech_detected'])
                self.assertEqual(diagnostic['duration_info_status'], 'unknown')
                json.dumps(diagnostic, allow_nan=False)

    def test_zero_transcript_title_helper_makes_no_api_request(self):
        self.assertEqual(new.gemini_titles('', 'not-a-real-key'), [])

    def test_preflight_exception_still_propagates_before_inference(self):
        model = Mock()
        with patch.object(preflight, 'check_input_decode', side_effect=RuntimeError('decode failure')), \
                patch.object(Path, 'write_text') as write, self.assertRaisesRegex(RuntimeError, 'decode failure'):
            new.transcribe_srt(model, 'clip.mp4', 'clip.srt', diagnostics={})
        model.transcribe.assert_not_called()
        write.assert_not_called()

    def test_lazy_generator_exception_still_propagates(self):
        def bad():
            yield segment()
            raise RuntimeError('decoder failed later')
        model = Mock()
        model.transcribe.return_value = (bad(), info())
        with patch.object(Path, 'write_text') as write, self.assertRaisesRegex(RuntimeError, 'decoder failed later'):
            new.transcribe_srt(model, 'clip.mp4', 'clip.srt', diagnostics={})
        write.assert_not_called()
        model.transcribe.assert_called_once()

    def run_main(self, rows, metadata, failure=None):
        request = {'platform': 'kick', 'video': 'approved-reference', 'segments':
                   [{'id': 1, 'start': 1, 'end': 11, 'score': 10, 'tags': ['keep']} ]}
        model = Mock()
        model.transcribe.side_effect = failure
        model.transcribe.return_value = (iter(rows), metadata)
        whisper = types.ModuleType('faster_whisper')
        whisper.WhisperModel = Mock(return_value=model)
        self.writes = []
        def save(path, text, **kwargs):
            self.writes.append((str(path), text))
        with patch.dict(sys.modules, {'faster_whisper': whisper}), \
                patch.object(sys, 'argv', ['shorts_prep.py']), \
                patch.dict('os.environ', {'GEMINI_API_KEY': 'stub-not-a-real-key'}), \
                patch.object(Path, 'read_text', return_value=json.dumps(request)), \
                patch.object(Path, 'write_text', autospec=True, side_effect=save), \
                patch.object(new, 'resolve_source', return_value=('stub-source', 'VOD title')), \
                patch.object(new, 'cut_clip') as cut, \
                patch.object(new, 'gemini_titles', return_value=['Title']) as title, \
                patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            caught = None
            try:
                new.main()
            except SystemExit as exc:
                caught = exc.code
        manifests = [json.loads(text) for path, text in self.writes if path.endswith('manifest.json')]
        self.assertEqual(len(manifests), 1)
        self.assertEqual(cut.call_count, 1)
        self.assertEqual(model.transcribe.call_count, 1)
        whisper.WhisperModel.assert_called_once_with('small', device='cpu', compute_type='int8')
        return manifests[0], title, caught

    def test_main_still_calls_title_helper_once_per_successful_clip(self):
        manifest, title, caught = self.run_main([segment()], info())
        self.assertIsNone(caught)
        title.assert_called_once_with('speech', 'stub-not-a-real-key')
        clip = manifest['clips'][0]
        self.assertEqual((clip['file'], clip['srt'], clip['score'], clip['tags']),
                         ('clip_01.mp4', 'clip_01.srt', 10, ['keep']))

    def test_main_error_behavior_and_manifest_save_are_unchanged(self):
        manifest, title, caught = self.run_main([], info(), RuntimeError('mock decoder error'))
        self.assertEqual(manifest['clips'], [])
        self.assertEqual(caught, 1)
        title.assert_not_called()

    def test_main_empty_subtitles_are_saved_with_explicit_review_status(self):
        manifest, title, caught = self.run_main([], info(after=0))
        self.assertIsNone(caught)
        self.assertEqual(len(manifest['clips']), 1)
        clip = manifest['clips'][0]
        self.assertEqual(clip['n_lines'], 0)
        self.assertEqual(clip['transcription']['status'], 'needs-audio-review')
        self.assertEqual(clip['transcription']['cause'], 'undetermined')
        self.assertTrue(clip['transcription']['vad_no_speech_detected'])
        self.assertEqual([text for path, text in self.writes if path.endswith('.srt')], [''])
        title.assert_called_once_with('', 'stub-not-a-real-key')


if __name__ == '__main__':
    unittest.main()
