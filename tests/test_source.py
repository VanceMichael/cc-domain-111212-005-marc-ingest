import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import app
from marc import parse_batch
from sampledata import samples
from source import inspect_bytes
from storage import Archive
from transport import ReplaySource, fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()
        self.name = 'sample.mrc'

    def test_domain_sample(self):
        data = self.fixtures[self.name]
        result = inspect_bytes(data)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['offset'], 0)
        self.assertEqual(result[1]['offset'], result[0]['length'])
        self.assertEqual(result[0]['encoding'], 'UTF-8')

    def test_invalid_sample(self):
        invalid = self.fixtures['invalid.mrc']
        with self.assertRaises(Exception):
            inspect_bytes(invalid)

    def test_sample_determinism(self):
        self.assertEqual(self.fixtures, samples())
        self.assertEqual(inspect_bytes(self.fixtures[self.name]),
                         inspect_bytes(self.fixtures[self.name]))

    def test_capture_is_content_scoped_and_durable(self):
        data = self.fixtures[self.name]
        batch = parse_batch(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            with Archive(path) as archive:
                first = archive.import_batch('source-a', data, batch)
                repeated = archive.import_batch('source-a', data, batch)
                self.assertEqual(repeated['delivery_id'], first['delivery_id'])
                self.assertTrue(repeated['duplicate_import'])
                other = archive.import_batch('source-b', data, batch)
                self.assertNotEqual(other['delivery_id'], first['delivery_id'])
                self.assertEqual(archive.raw(first['delivery_id']), data)
            with Archive(path) as archive:
                self.assertEqual(len(archive.list()), 2)
                record = archive.list()[0]['records'][0]
                self.assertEqual(record['identity']['control_001'], 'bk-1')
                self.assertEqual(record['outcome'], 'new')

    def test_empty_source_rejected_without_writing(self):
        with Archive(':memory:') as archive:
            with self.assertRaises(ValueError):
                archive.import_batch(' ', b'raw', {'complete': True, 'records': []})
            self.assertEqual(archive.list(), [])

    def test_empty_batch_rejected(self):
        with Archive(':memory:') as archive:
            with self.assertRaises(ValueError):
                archive.import_batch('source-a', b'', parse_batch(b''))
            self.assertEqual(archive.list(), [])

    def test_untrusted_batch_is_not_saved(self):
        data = self.fixtures['invalid.mrc']
        batch = parse_batch(data)
        self.assertFalse(batch['complete'])
        with Archive(':memory:') as archive:
            with self.assertRaises(ValueError):
                archive.import_batch('source-a', data, batch)
            self.assertEqual(archive.list(), [])

    def test_real_http_and_chunk_boundaries(self):
        data = self.fixtures[self.name]
        route = dict(base64=base64.b64encode(data).decode(), chunk_bytes=1)
        with ReplaySource({'GET /source': [route]}) as server:
            status, headers, raw = fetch(server.url + '/source')
            self.assertEqual(status, 200)
            self.assertEqual(raw, data)
            self.assertEqual(inspect_bytes(raw), inspect_bytes(data))
            self.assertEqual(len(server.requests), 1)

    def test_http_status_and_limit(self):
        with ReplaySource({'GET /sequence': [dict(status=429, text='cooldown'),
                                             dict(text='ok')]}) as server:
            self.assertEqual(fetch(server.url + '/sequence')[0], 429)
            self.assertEqual(fetch(server.url + '/sequence')[2], b'ok')
            with self.assertRaises(ValueError):
                fetch(server.url + '/sequence', max_bytes=1)

    def test_cli_capture_list_and_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.name
            path.write_bytes(self.fixtures[self.name])
            database = str(Path(directory) / 'store.sqlite3')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.main(['capture', str(path), '--database',
                                           database, '--source', 'cli']), 0)
            captured = json.loads(output.getvalue())
            self.assertEqual(captured['record_count'], 2)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.main(['list', '--database', database]), 0)
            self.assertEqual(len(json.loads(output.getvalue())), 1)
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(app.main(['inspect', str(path) + '.missing']), 2)
            self.assertIn('FileNotFoundError', error.getvalue())

    def test_cli_reports_boundary_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.mrc'
            path.write_bytes(self.fixtures['invalid.mrc'])
            database = str(Path(directory) / 'store.sqlite3')
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(app.main(['capture', str(path), '--database',
                                           database, '--source', 'cli']), 2)
            payload = json.loads(error.getvalue())
            self.assertEqual(payload['code'], 'record_truncated')
            self.assertFalse(payload['trusted'])
            with Archive(database) as archive:
                self.assertEqual(archive.list(), [])

    def test_cli_structure_and_showcase(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'repeated.mrc'
            path.write_bytes(self.fixtures['repeated.mrc'])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.main(['structure', str(path)]), 0)
            report = json.loads(output.getvalue())
            record = report['records'][0]
            self.assertEqual(record['leader']['record_length'], len(self.fixtures['repeated.mrc']))
            self.assertEqual(len(record['directory']['entries']), 9)
            self.assertEqual(len(record['control_fields']), 2)
            self.assertEqual(len([field for field in record['data_fields']
                                  if field['tag'] == '700']), 3)
            report = app.showcase()
            self.assertEqual(report['chinese_length']['characters'], 10)
            self.assertEqual(report['chinese_length']['utf8_bytes'], 28)
            self.assertFalse(report['truncated_tail']['complete'])
            self.assertEqual(report['truncated_tail']['parsed_before_stop'], ['bk-20'])
            self.assertEqual(report['recoverable_middle']['statuses'],
                             ['ok', 'invalid', 'ok'])

    def test_demo_reaches_source_and_archive(self):
        report = app.demo()
        self.assertEqual(report['deliveries'], 3)
        self.assertEqual(report['requests'], 5)
        self.assertTrue(report['same_delivery'])
        self.assertEqual(report['outcomes']['revision'], ['revision'])
        self.assertEqual(report['outcomes']['resend'], ['resend'])
        self.assertEqual(report['repeated_fields'], ['李', '陈', '王'])
        self.assertEqual(report['chinese'], {'characters': 4, 'bytes': 12})
        self.assertFalse(report['truncated']['complete'])
        self.assertEqual(report['truncated']['parsed'], ['bk-20'])


if __name__ == '__main__':
    unittest.main()
