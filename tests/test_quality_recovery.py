from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from lubo.quality_worker import RecognitionLimitError, audio_duration, recognize_with_recovery


class RecognitionRecoveryTests(unittest.TestCase):
    def make_wave(self, folder, seconds):
        path = Path(folder) / 'input.wav'
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * (16000 * seconds))
        return path

    def test_failed_long_decoding_is_retried_without_losing_audio(self):
        lengths = []
        with TemporaryDirectory() as folder:
            path = self.make_wave(folder, 20)
            def decode(sample):
                duration = audio_duration(sample['audio'])
                if duration > 12:
                    raise RecognitionLimitError()
                lengths.append(duration)
                return {'text': '前半句' if sample['id'].endswith('.0') else '后半句', 'language': 'Chinese'}
            result = recognize_with_recovery({'id': 'clip', 'audio': str(path)}, decode, lambda _: None)
        self.assertEqual(result['text'], '前半句 后半句')
        self.assertTrue(result['recovered'])
        self.assertEqual(result['recovery_parts'], 2)
        self.assertEqual(sum(lengths), 20)

    def test_unrecoverable_audio_is_not_reported_as_success(self):
        with TemporaryDirectory() as folder:
            path = self.make_wave(folder, 8)
            def decode(sample):
                raise RecognitionLimitError()
            with self.assertRaises(RecognitionLimitError):
                recognize_with_recovery({'id': 'clip', 'audio': str(path)}, decode, lambda _: None)

    def test_unrelated_errors_are_not_hidden_as_retry(self):
        def decode(sample):
            raise FileNotFoundError('missing audio')
        with self.assertRaises(FileNotFoundError):
            recognize_with_recovery({'id': 'clip', 'audio': 'missing.wav'}, decode, lambda _: None)


if __name__ == '__main__':
    unittest.main()
