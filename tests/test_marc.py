"""字节级结构测试：只依赖声明长度和原始切片，不依赖解码后的字符数。"""

import unittest

from marc import BoundaryError, parse_batch, parse_record
from sampledata import make_record, record, samples


class StructureTests(unittest.TestCase):
    def test_leader_directory_and_field_boundaries(self):
        batch = parse_batch(samples()['sample.mrc'])
        first = batch['records'][0]
        self.assertEqual(first['offset'], 0)
        self.assertEqual(first['length'], 146)
        self.assertEqual(first['base'], 85)
        self.assertEqual(first['leader']['record_length'], 146)
        self.assertEqual(first['leader']['base_address'], 85)
        self.assertEqual(first['directory']['start'], 24)
        self.assertEqual(first['directory']['end'], 84)
        self.assertEqual(first['directory']['entry_length'], 12)
        self.assertEqual([entry['tag'] for entry in first['directory']['entries']],
                         ['001', '003', '245', '700', '700'])
        # 每个目录项声明的长度必须等于字段切片长度，并与下一项起点无缝衔接。
        previous_end = first['base']
        for entry in first['directory']['entries']:
            self.assertEqual(entry['absolute_start'], previous_end)
            self.assertEqual(entry['absolute_end'] - entry['absolute_start'],
                             entry['declared_length'])
            field = [item for item in first['fields']
                     if item['directory_position'] == entry['ordinal']][0]
            self.assertEqual(first['raw']['hex'][entry['absolute_start'] * 2:
                                                  entry['absolute_end'] * 2],
                             field['evidence']['hex'])
            self.assertEqual(entry['evidence']['start'], 24 + entry['ordinal'] * 12)
            previous_end = entry['absolute_end']
        # 第二条记录从第一条声明长度处开始。
        self.assertEqual(batch['records'][1]['offset'], first['length'])

    def test_control_fields_have_no_indicators(self):
        record = parse_batch(samples()['sample.mrc'])['records'][0]
        control = record['control_fields']
        self.assertEqual([field['tag'] for field in control], ['001', '003'])
        self.assertTrue(all(field['indicators'] is None for field in control))
        self.assertEqual(control[0]['value'], 'bk-1')
        self.assertEqual(control[0]['field_length'], 5)

    def test_data_field_indicators_subfields_and_order(self):
        record = parse_batch(samples()['repeated.mrc'])['records'][0]
        title = record['data_fields'][0]
        self.assertEqual(title['tag'], '245')
        self.assertEqual(title['indicators'], '10')
        self.assertEqual([item['value'] for item in title['indicator_positions']],
                         ['1', '0'])
        self.assertEqual([sub['code'] for sub in title['subfields']], ['a', 'b'])
        self.assertEqual([sub['ordinal'] for sub in title['subfields']], [0, 1])
        contributors = [field for field in record['data_fields'] if field['tag'] == '700']
        self.assertEqual(len(contributors), 3)
        self.assertEqual([[sub['code'] for sub in field['subfields']]
                          for field in contributors],
                         [['a', 'e', 'e'], ['a', 'e'], ['a', 'e']])
        # 重复子字段保序，并且每一份都有独立位置证据。
        first = contributors[0]
        self.assertEqual([sub['value'] for sub in first['subfields']],
                         ['李', '主编', '译者'])
        starts = [sub['evidence']['start'] for sub in first['subfields']]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(first['subfields'][1]['evidence']['hex'],
                         '1f65e4b8bbe7bc96')
        # 子字段切片的 hex 与原始记录偏移一致。
        raw_hex = record['raw']['hex']
        for sub in first['subfields']:
            evidence = sub['evidence']
            self.assertEqual(raw_hex[evidence['start'] * 2:evidence['end'] * 2],
                             evidence['hex'])

    def test_chinese_length_uses_bytes_not_characters(self):
        record = parse_batch(samples()['repeated.mrc'])['records'][0]
        title = record['export']['title']
        exported = [field for field in record['data_fields'] if field['tag'] == '245'][0]
        declared = exported['declared_length']
        self.assertEqual(len('汉语对照手册 附索引'), 10)
        self.assertEqual(len('汉语对照手册 附索引'.encode('utf-8')), 28)
        # 17 = 2 指标 + 0x1f + 1 代码 + 14 字节 + 0x1f + 1 代码 + 8 字节 + 0x1e
        self.assertEqual(title['characters'], 10)
        self.assertEqual(title['bytes'], 28)
        self.assertEqual(declared, 34)
        self.assertEqual(exported['field_end'] - exported['field_start'], declared)

    def test_export_returns_to_field_positions(self):
        record = parse_batch(samples()['repeated.mrc'])['records'][0]
        export = record['export']
        fields = record['fields']
        self.assertEqual(export['title']['field_position'],
                         [index for index, field in enumerate(fields)
                          if field['tag'] == '245'][0])
        self.assertEqual(export['title']['field_start'],
                         fields[export['title']['field_position']]['field_start'])
        self.assertEqual([item['field_position'] for item in export['contributors']],
                         [index for index, field in enumerate(fields)
                          if field['tag'] == '700'])
        self.assertEqual(len(export['contributors']), 3)
        self.assertEqual(export['contributors'][0]['roles'], ['主编', '译者'])
        self.assertEqual(export['publication'][0]['place'], '北京')
        self.assertEqual(export['publication'][0]['publisher'], '联合出版社')
        self.assertEqual(export['publication'][0]['date'], '2024')

    def test_identity_is_001_with_003(self):
        records = parse_batch(samples()['agencies.mrc'])['records']
        self.assertEqual(records[0]['identity'],
                         {'control_001': 'shared-1', 'agency_003': 'AAA',
                          'basis': '001+003+source'})
        self.assertEqual(records[1]['identity']['agency_003'], 'BBB')

    def test_unsupported_charset_is_isolated_not_replaced(self):
        record = parse_batch(samples()['latin1.mrc'])['records'][0]
        self.assertEqual(record['status'], 'unsupported_encoding')
        self.assertEqual(record['leader']['coding_scheme'], ' ')
        codes = [error['code'] for error in record['errors']]
        self.assertEqual(codes, ['unsupported_encoding'])
        title = [field for field in record['data_fields'] if field['tag'] == '245'][0]
        self.assertIsNone(title['subfields'][0]['value'])
        self.assertNotIn('�', str(record))
        self.assertNotIn('Café', str(record))

    def test_invalid_utf8_is_error_not_replacement(self):
        broken = bytearray(make_record('bad', '标题'))
        # 把 245 里第一个汉字的首字节破坏掉，长度声明保持不变。
        marker = '标题'.encode('utf-8')
        broken[bytes(broken).index(marker)] = 0xFF
        batch = parse_batch(bytes(broken))
        record = batch['records'][0]
        self.assertEqual(record['status'], 'invalid')
        codes = [error['code'] for error in record['errors']]
        self.assertIn('invalid_utf8', codes)
        error = next(error for error in record['errors'] if error['code'] == 'invalid_utf8')
        self.assertEqual(error['bad_hex'], 'ff')
        title = record['export']['title']
        self.assertIsNone(title['value'])
        self.assertIsNone(title['subfields'][0]['value'])
        self.assertNotIn('�', str(record))

    def test_recoverable_inner_error_continues(self):
        batch = parse_batch(samples()['mixed.mrc'])
        self.assertTrue(batch['complete'])
        self.assertEqual(len(batch['records']), 3)
        self.assertEqual([record['status'] for record in batch['records']],
                         ['ok', 'invalid', 'ok'])
        self.assertEqual(batch['records'][1]['errors'][0]['code'],
                         'directory_entry_invalid')
        self.assertEqual(batch['records'][2]['identity']['control_001'], 'bk-12')
        self.assertEqual(batch['records'][2]['export']['title']['value'], '后续记录')

    def test_truncated_tail_stops_with_evidence(self):
        batch = parse_batch(samples()['truncated-tail.mrc'])
        self.assertFalse(batch['complete'])
        self.assertEqual(len(batch['records']), 1)
        stopped = batch['stopped']
        self.assertEqual(stopped['code'], 'record_truncated')
        self.assertEqual(stopped['offset'], batch['consumed'])
        self.assertEqual(stopped['declared_length'], 144)
        self.assertEqual(stopped['available'], 140)
        self.assertFalse(stopped['trusted'])
        self.assertEqual(batch['remaining'], 140)

    def test_truncated_first_record_halts_without_scanning(self):
        data = samples()['invalid.mrc']
        batch = parse_batch(data)
        self.assertEqual(batch['records'], [])
        self.assertFalse(batch['complete'])
        self.assertEqual(batch['stopped']['code'], 'record_truncated')
        self.assertEqual(batch['remaining'], len(data))
        with self.assertRaises(BoundaryError):
            parse_record(data)

    def test_declared_terminator_mismatch_is_boundary_error(self):
        data = bytearray(make_record('term', '结束符'))
        data[-1] = 0x20
        with self.assertRaises(BoundaryError) as caught:
            parse_record(bytes(data))
        self.assertEqual(caught.exception.code, 'record_terminator_mismatch')
        self.assertFalse(caught.exception.as_dict()['trusted'])

    def test_missing_field_terminator_is_record_error_not_boundary(self):
        data = bytearray(record([('001', b'x'), ('245', b'10\x1faT\x1e')]))
        # 这里只检查正常构造的夹具没有误报；然后破坏 245 的字段终止符。
        data[-2] = ord('!')
        parsed = parse_batch(bytes(data))['records'][0]
        self.assertEqual(parsed['status'], 'invalid')
        self.assertIn('field_terminator_missing',
                      [error['code'] for error in parsed['errors']])
        # 外层长度仍可靠，因此记录本身被保留，可继续后面的记录。
        self.assertTrue(parse_batch(bytes(data))['complete'])

    def test_directory_lengths_not_characters_drive_offsets(self):
        text = '汉字标题'
        raw = make_record('len', text)
        parsed = parse_batch(raw)['records'][0]
        self.assertEqual(len(text), 4)
        self.assertEqual(len(text.encode('utf-8')), 12)
        field = [item for item in parsed['data_fields'] if item['tag'] == '245'][0]
        self.assertEqual(field['declared_length'], 17)
        self.assertEqual(field['evidence']['length'], 17)
        # 若按字符数计算，字段边界会落到 13 而不是 17/起点之后。
        self.assertNotEqual(field['declared_length'], 2 + 1 + 1 + len(text) + 1)


if __name__ == '__main__':
    unittest.main()
