import tempfile
import unittest
from pathlib import Path

from runtime_input_resolver import normalize_ocr_value, resolve_image_captcha


class RuntimeInputResolverTests(unittest.TestCase):
    def test_normalize_ocr_value_removes_noise_but_preserves_alphanumeric_text(self):
        self.assertEqual(normalize_ocr_value(' c9-G H\n'), 'c9GH')

    def test_resolve_image_captcha_returns_structured_runtime_value(self):
        class FakeClassifier:
            def classification(self, image_bytes):
                self.image_bytes = image_bytes
                return ' A 7-b '

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / 'captcha.png'
            image_path.write_bytes(b'captcha-image')

            result = resolve_image_captcha(image_path, classifier_factory=FakeClassifier)

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['resolver'], 'image_ocr')
        self.assertEqual(result['value'], 'A7b')
