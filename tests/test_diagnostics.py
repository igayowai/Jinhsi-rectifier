import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rectifier import diagnostics


class ErrorRecordTests(TestCase):
    def test_only_place_and_type_name_are_written(self):
        with TemporaryDirectory() as tmp, patch.object(diagnostics, 'ENABLED', True), \
                patch.object(diagnostics, 'ERROR_PATH', Path(tmp) / 'errors.jsonl'):
            try:
                raise ValueError('private sentinel password=secret')
            except ValueError:
                self.assertTrue(diagnostics.record_error('module.function'))
            text = diagnostics.ERROR_PATH.read_text(encoding='utf-8')
        self.assertNotIn('sentinel', text)
        self.assertNotIn('secret', text)
        record = json.loads(text)
        self.assertEqual((record['where'], record['type'], record['kind']),
                         ('module.function', 'ValueError', 'swallowed_exception'))

    def test_disabled_or_failing_writer_never_raises(self):
        self.assertFalse(diagnostics.record_error('x', RuntimeError()))  # Disabled under tests.
        with patch.object(diagnostics, 'ENABLED', True), \
                patch.object(diagnostics, 'write_summary', side_effect=OSError):
            self.assertFalse(diagnostics.record_error('x', RuntimeError()))
