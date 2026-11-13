"""收件身份与书目身份测试：重复批次、同控制号新内容、原件重送、跨馆同 001。"""

import tempfile
import unittest

from marc import parse_batch
from sampledata import samples
from storage import Archive


def outcomes(report):
    return [(item['record_index'], item['outcome']) for item in report['records']]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()
        self.directory = tempfile.TemporaryDirectory()
        self.database = self.directory.name + '/store.sqlite3'

    def tearDown(self):
        self.directory.cleanup()

    def _import(self, archive, source, name):
        return archive.import_batch(source, self.fixtures[name],
                                    parse_batch(self.fixtures[name]))

    def test_repeated_delivery_keeps_one_recipient_identity(self):
        with Archive(self.database) as archive:
            first = self._import(archive, 'vendor-a', 'sample.mrc')
            second = self._import(archive, 'vendor-a', 'sample.mrc')
            third = self._import(archive, 'vendor-a', 'resend.mrc')
            fourth = self._import(archive, 'vendor-a', 'resend.mrc')
        self.assertEqual(first['delivery_id'], second['delivery_id'])
        self.assertNotEqual(first['delivery_id'], third['delivery_id'])
        self.assertEqual(third['delivery_id'], fourth['delivery_id'])
        self.assertFalse(first['duplicate_import'])
        self.assertTrue(second['duplicate_import'])
        self.assertFalse(third['duplicate_import'])
        self.assertTrue(fourth['duplicate_import'])
        self.assertEqual(outcomes(third), [(0, 'resend')])

    def test_same_control_new_content_versus_resend(self):
        with Archive(self.database) as archive:
            original = self._import(archive, 'vendor-a', 'sample.mrc')
            revision = self._import(archive, 'vendor-a', 'revision.mrc')
            resend = self._import(archive, 'vendor-a', 'resend.mrc')
            second_resend = self._import(archive, 'vendor-a', 'resend.mrc')
            revision_again = self._import(archive, 'vendor-a', 'revision.mrc')
        self.assertEqual(outcomes(original), [(0, 'new'), (1, 'new')])
        self.assertEqual(outcomes(revision), [(0, 'revision')])
        self.assertEqual(revision['records'][0]['record_id'],
                         original['records'][0]['record_id'])
        self.assertNotEqual(revision['records'][0]['revision_id'],
                            original['records'][0]['revision_id'])
        self.assertEqual(outcomes(resend), [(0, 'resend')])
        self.assertEqual(resend['records'][0]['revision_id'],
                         original['records'][0]['revision_id'])
        self.assertTrue(second_resend['duplicate_import'])
        self.assertEqual(second_resend['delivery_id'], resend['delivery_id'])
        self.assertTrue(revision_again['duplicate_import'])
        self.assertEqual(revision_again['delivery_id'], revision['delivery_id'])

    def test_same_001_in_different_agencies_does_not_overwrite(self):
        with Archive(self.database) as archive:
            agencies = self._import(archive, 'vendor-a', 'agencies.mrc')
            export = archive.export(agencies['delivery_id'])
        self.assertEqual(outcomes(agencies), [(0, 'new'), (1, 'new')])
        identities = [dict(record['identity'], record_id=record['record_id'])
                      for record in export]
        self.assertEqual(identities[0]['control_001'], 'shared-1')
        self.assertEqual(identities[0]['agency_003'], 'AAA')
        self.assertEqual(identities[1]['control_001'], 'shared-1')
        self.assertEqual(identities[1]['agency_003'], 'BBB')
        self.assertNotEqual(identities[0]['record_id'], identities[1]['record_id'])
        # 甲馆的标题没有被乙馆覆盖。
        self.assertEqual(export[0]['export']['title']['value'], '甲馆记录')
        self.assertEqual(export[1]['export']['title']['value'], '乙馆记录')

    def test_same_001_from_different_source_does_not_overwrite(self):
        with Archive(self.database) as archive:
            first = self._import(archive, 'vendor-a', 'sample.mrc')
            second = self._import(archive, 'vendor-b', 'sample.mrc')
        self.assertNotEqual(first['delivery_id'], second['delivery_id'])
        self.assertNotEqual(first['records'][0]['record_id'],
                            second['records'][0]['record_id'])

    def test_duplicate_control_inside_one_batch(self):
        data = self.fixtures['resend.mrc'] + self.fixtures['resend.mrc']
        batch = parse_batch(data)
        with Archive(':memory:') as archive:
            report = archive.import_batch('vendor-a', data, batch)
        self.assertEqual(outcomes(report), [(0, 'new'), (1, 'resend')])
        self.assertEqual(report['records'][1]['revision_id'],
                         report['records'][0]['revision_id'])

    def test_invalid_record_is_kept_as_evidence_but_not_revision(self):
        with Archive(':memory:') as archive:
            report = self._import(archive, 'vendor-a', 'mixed.mrc')
            revision_rows = archive.connection.execute(
                'SELECT COUNT(*) FROM revisions').fetchone()[0]
            exports = archive.export()
        self.assertEqual(outcomes(report), [(0, 'new'), (1, 'invalid'), (2, 'new')])
        self.assertEqual(revision_rows, 2)
        self.assertEqual(len(exports), 2)
        self.assertEqual([item['record_index'] for item in exports], [0, 2])
        self.assertEqual(report['records'][1]['error_codes'],
                         ['directory_entry_invalid', 'field_region_gap_or_overlap'])
        self.assertIsNone(report['records'][1]['revision_id'])

    def test_unsupported_encoding_record_is_isolated(self):
        with Archive(':memory:') as archive:
            report = self._import(archive, 'vendor-a', 'latin1.mrc')
            exports = archive.export()
        self.assertEqual(outcomes(report), [(0, 'unsupported_encoding')])
        self.assertEqual(exports, [])
        # 控制号是 ASCII，可用于隔离清单；标题等文本仍不解码。
        self.assertEqual(report['records'][0]['identity']['control_001'], 'latin-1')
        self.assertEqual(report['records'][0]['identity']['agency_003'], 'LOCAL')
        self.assertIsNone(report['records'][0]['revision_id'])
        self.assertNotIn('�', str(report))

    def test_export_returns_to_field_positions_after_roundtrip(self):
        data = self.fixtures['repeated.mrc']
        batch = parse_batch(data)
        with Archive(self.database) as archive:
            archive.import_batch('vendor-a', data, batch)
            export = archive.export()[0]
            parsed = archive.parse_report(1)[0]
        self.assertEqual(export['export'], parsed['export'])
        record_raw = data[export['raw']['offset']:export['raw']['offset'] + export['raw']['length']]
        import hashlib
        self.assertEqual(hashlib.sha256(record_raw).hexdigest(), export['raw']['sha256'])
        title = export['export']['title']
        self.assertEqual(record_raw[title['field_start'] - export['raw']['offset']:
                                    title['field_end'] - export['raw']['offset']][-1], 0x1e)
        for field in export['export']['contributors'] + export['export']['publication']:
            self.assertEqual(field['field_position'],
                             [index for index, item in enumerate(parsed['fields'])
                              if item['field_start'] == field['field_start']][0])

    def test_incomplete_batch_never_writes(self):
        data = self.fixtures['truncated-tail.mrc']
        batch = parse_batch(data)
        with Archive(':memory:') as archive:
            with self.assertRaises(Exception):
                archive.import_batch('vendor-a', data, batch)
            self.assertEqual(archive.list(), [])


if __name__ == '__main__':
    unittest.main()
