"""按声明字节长度解析 ISO 2709 / MARC21 记录。

解析器只相信记录自身声明的边界：

* leader 的 00-04 位声明记录总长，17-19 位（record_length - 1）必须是
  0x1d。两者不能同时成立时，外层边界不可信，调用方必须停止，不能向后
  扫描其它结束符猜测记录起点。
* 一旦外层边界可信，目录项声明的字段长度和起点负责定位每个字段；字段
  内部再用 0x1f 定位子字段。解码后的字符数从不参与定位。
* leader/09 只接受 ``a``（UTF-8）。其它字符集按记录隔离，不尝试用
  latin1 或替换字符兜底。

每个解析单元都带有原始字节切片（绝对偏移、长度、十六进制摘要），因此后续
导出可以回到字段/子字段的出现位置。
"""

import hashlib
import json

LEADER_LENGTH = 24
FIELD_TERMINATOR = 0x1E
RECORD_TERMINATOR = 0x1D
SUBFIELD_DELIMITER = 0x1F
MIN_RECORD_LENGTH = 26
UTF8_CODING = 'a'

ENCODING_LABELS = {
    'a': 'UTF-8',
    ' ': 'MARC-8',
    'A': 'MARC-8',
    'b': 'ISO-5426',
    'c': 'ISO-2022',
}

CONTRIBUTOR_TAGS = ('100', '110', '111', '700', '710', '711')
PUBLICATION_TAGS = ('260', '264')
TITLE_TAG = '245'


class MarcError(ValueError):
    """MARC 结构或编码错误。"""


class BoundaryError(MarcError):
    """外层记录边界不可信；调用方必须停止，而不是扫描猜测。"""

    def __init__(self, code, reason, offset, **detail):
        self.code = code
        self.reason = reason
        self.offset = offset
        self.detail = dict(detail)
        super().__init__(reason)

    def as_dict(self):
        report = {'code': self.code, 'reason': self.reason, 'offset': self.offset,
                  'trusted': False}
        report.update(self.detail)
        return report


def _evidence(data, start, end):
    """返回一段原始字节的可定位证据。"""
    payload = bytes(data[start:end])
    return {'start': start, 'end': end, 'length': end - start,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'hex': payload.hex()}


def _ascii_int(raw):
    raw = bytes(raw)
    if not raw:
        return None
    for byte in raw:
        if byte < 0x30 or byte > 0x39:
            return None
    return int(raw)


def _strict_decode(raw, allowed, codec='utf-8'):
    """严格解码；损坏时返回 (None, error)，绝不产生 U+FFFD 文本。"""
    if not allowed:
        return None, None
    try:
        return bytes(raw).decode(codec), None
    except UnicodeDecodeError as error:
        label = 'UTF-8' if codec == 'utf-8' else codec.upper()
        return None, {
            'code': 'invalid_utf8' if codec == 'utf-8' else 'invalid_ascii',
            'reason': '%s 解码失败：字节 %d 处 %s' % (label, error.start, error.reason),
            'byte_offset': error.start,
            'bad_hex': bytes(raw[error.start:error.end]).hex(),
        }


def _leader_details(leader):
    raw = bytes(leader)

    def char(index):
        return chr(raw[index]) if index < len(raw) else None

    coding = char(9)
    return {
        'raw': _evidence(raw, 0, len(raw)),
        'record_length': _ascii_int(raw[0:5]),
        'record_status': char(5),
        'type_of_record': char(6),
        'bibliographic_level': char(7),
        'type_of_control': char(8),
        'coding_scheme': coding,
        'encoding': ENCODING_LABELS.get(coding, '未知字符集'),
        'indicator_count': _ascii_int(raw[10:11]),
        'subfield_code_length': _ascii_int(raw[11:12]),
        'base_address': _ascii_int(raw[12:17]),
        'encoding_level': char(17),
        'descriptive_cataloging_form': char(18),
        'multipart_level': char(19),
        'entry_map': bytes(raw[20:24]).decode('ascii', 'backslashreplace'),
    }


def _identity(fields):
    """001 只在来源与 003 机构的上下文里解释；单独的 001 不足以识别书目。"""
    control = next((field['value'] for field in fields
                    if field['tag'] == '001' and field.get('value') is not None), None)
    agency = next((field['value'] for field in fields
                   if field['tag'] == '003' and field.get('value') is not None), None)
    if control is None:
        basis = 'unresolved'
    elif agency is None:
        basis = '001+source'
    else:
        basis = '001+003+source'
    return {'control_001': control, 'agency_003': agency, 'basis': basis}


def _subfields(data, rest_start, rest_end, code_length, allowed, add_error, tag):
    """按 0x1f 拆分，保留重复子字段及每个子字段的绝对位置。

    ``rest_start``/``rest_end`` 是子字段区在 ``data`` 中的绝对字节边界；
    所有位置都相对记录开头，可直接回到原始切片。
    """
    rest = bytes(data[rest_start:rest_end])
    delimiters = [index for index, byte in enumerate(rest) if byte == SUBFIELD_DELIMITER]
    result = []
    prefix = rest[:delimiters[0]] if delimiters else rest
    if prefix:
        value, error = _strict_decode(prefix, allowed)
        if error:
            add_error(error)
        result.append({'kind': 'prefix', 'code': None, 'ordinal': len(result),
                       'value': value, 'evidence': _evidence(data, rest_start,
                                                             rest_start + len(prefix))})
    for ordinal, index in enumerate(delimiters):
        end = delimiters[ordinal + 1] if ordinal + 1 < len(delimiters) else len(rest)
        code_end = index + 1 + code_length
        if code_end > end:
            add_error({'code': 'subfield_code_truncated',
                       'reason': '子字段代码不完整',
                       'position': rest_start + index})
            code = None
            value_start = end
        else:
            code = bytes(rest[index + 1:code_end]).decode('ascii', 'backslashreplace')
            value_start = code_end
        value, error = _strict_decode(rest[value_start:end], allowed)
        if error:
            add_error(error)
        result.append({'kind': 'subfield', 'code': code, 'ordinal': len(result),
                       'value': value,
                       'evidence': _evidence(data, rest_start + index,
                                             rest_start + end)})
    return result


def _parse_directory(data, offset, base, leader, errors):
    leader_raw = bytes(data[offset:offset + LEADER_LENGTH])
    directory_start = offset + LEADER_LENGTH
    directory_end = offset + base - 1
    entries = []

    if base < LEADER_LENGTH + 1 or directory_end > offset + leader['record_length'] - 1:
        errors.append({'code': 'base_out_of_range', 'reason': '字段起点越界',
                       'base': base, 'record_length': leader['record_length']})
        return entries, None
    directory_region = data[directory_start:directory_end]
    if data[directory_end] != FIELD_TERMINATOR:
        errors.append({'code': 'directory_terminator_missing',
                       'reason': '目录末尾缺少 0x1e', 'position': directory_end})

    field_length_digits = _ascii_int(leader_raw[20:21])
    start_digits = _ascii_int(leader_raw[21:22])
    extra_digits = _ascii_int(leader_raw[22:24])
    if (field_length_digits is None or start_digits is None or extra_digits is None
            or field_length_digits < 1 or start_digits < 1):
        errors.append({'code': 'entry_map_invalid',
                       'reason': 'leader 20-23 位不是可解释的目录项长度图',
                       'entry_map': bytes(leader_raw[20:24]).decode('ascii', 'backslashreplace')})
        return entries, None
    entry_length = 3 + field_length_digits + start_digits + extra_digits
    if len(directory_region) % entry_length:
        errors.append({'code': 'directory_length_not_aligned',
                       'reason': '目录长度不是目录项长度的整数倍',
                       'directory_length': len(directory_region),
                       'entry_length': entry_length})
    usable = len(directory_region) - (len(directory_region) % entry_length)
    for index in range(0, usable, entry_length):
        raw_entry = bytes(directory_region[index:index + entry_length])
        tag = raw_entry[0:3].decode('ascii', 'backslashreplace')
        length_start = 3
        offset_start = length_start + field_length_digits
        entry = {
            'ordinal': len(entries),
            'tag': tag,
            'entry_length': entry_length,
            'field_length_digits': field_length_digits,
            'start_position_digits': start_digits,
            'implementation': raw_entry[offset_start + start_digits:].hex(),
            'evidence': _evidence(data, directory_start + index,
                                  directory_start + index + entry_length),
        }
        field_length = _ascii_int(raw_entry[length_start:offset_start])
        field_start = _ascii_int(raw_entry[offset_start:offset_start + start_digits])
        if field_length is None or field_start is None:
            entry.update(declared_length=None, declared_offset=None,
                         error='目录项长度或起点不是数字')
            errors.append({'code': 'directory_entry_invalid', 'reason': entry['error'],
                           'tag': tag, 'entry_position': directory_start + index})
            entries.append(entry)
            continue
        absolute_start = offset + base + field_start
        absolute_end = absolute_start + field_length
        entry.update(declared_length=field_length, declared_offset=field_start,
                     absolute_start=absolute_start, absolute_end=absolute_end)
        if (field_start < 0 or field_length < 1
                or absolute_end > offset + leader['record_length'] - 1):
            entry['error'] = '字段切片越过记录边界'
            errors.append({'code': 'field_out_of_bounds', 'reason': entry['error'],
                           'tag': tag, 'field_start': absolute_start,
                           'field_end': absolute_end})
        elif data[absolute_end - 1] != FIELD_TERMINATOR:
            entry['error'] = '字段末尾缺少 0x1e'
            errors.append({'code': 'field_terminator_missing',
                           'reason': entry['error'], 'tag': tag,
                           'position': absolute_end - 1})
        entries.append(entry)

    # 字段必须按目录声明的起点无缝覆盖字段区，不能有重叠或空洞。
    spans = sorted((entry['absolute_start'], entry['absolute_end'])
                   for entry in entries if entry.get('absolute_start') is not None)
    expected = offset + base
    field_region_end = offset + leader['record_length'] - 1
    for start, end in spans:
        if start != expected:
            errors.append({'code': 'field_region_gap_or_overlap',
                           'reason': '字段切片没有连续覆盖字段区',
                           'expected_start': expected, 'actual_start': start})
            break
        expected = end
    else:
        if spans and expected != field_region_end:
            errors.append({'code': 'field_region_gap_or_overlap',
                           'reason': '字段区末尾与记录结束符之间有空隙',
                           'expected_end': field_region_end, 'actual_end': expected})
    return entries, entry_length


def _parse_field(data, entry, leader, allowed, errors):
    tag = entry['tag']
    start = entry['absolute_start']
    end = entry['absolute_end']
    raw_field = data[start:end]
    payload = raw_field[:-1] if raw_field and raw_field[-1] == FIELD_TERMINATOR else raw_field
    field = {
        'tag': tag,
        'directory_position': entry['ordinal'],
        'field_start': start,
        'field_end': end,
        'field_length': entry['declared_length'],
        'declared_length': entry['declared_length'],
        'declared_offset': entry['declared_offset'],
        'evidence': _evidence(data, start, end),
        'indicators': None,
        'indicator_positions': [],
        'subfields': [],
        'value': None,
        'kind': 'control' if tag.startswith('00') else 'data',
    }
    if tag.startswith('00'):
        if allowed:
            value, error = _strict_decode(payload, True)
        elif tag in ('001', '003'):
            # 001/003 在 ISO 2709 中定义为 ASCII，可在隔离记录时用于定位，
            # 但只做严格 ASCII，不借用其它字符集解释整条记录。
            value, error = _strict_decode(payload, True, 'ascii')
        else:
            value, error = None, None
        if error:
            errors.append(dict(error, tag=tag, field_start=start))
        field['value'] = value
        return field

    indicator_count = leader.get('indicator_count')
    identifier_length = leader.get('subfield_code_length')
    if indicator_count is None or identifier_length is None:
        errors.append({'code': 'leader_indicator_map_invalid',
                       'reason': 'leader 10-11 位不能解释为指标数/子字段代码长度',
                       'tag': tag})
        indicator_count, identifier_length = 0, 2
    if len(payload) < indicator_count:
        errors.append({'code': 'indicators_truncated', 'reason': '指标位不完整',
                       'tag': tag, 'field_start': start})
        return field
    indicator_raw = bytes(payload[:indicator_count])
    if any(byte > 0x7E or byte < 0x20 for byte in indicator_raw):
        errors.append({'code': 'indicators_not_ascii', 'reason': '指标位不是 ASCII',
                       'tag': tag, 'hex': indicator_raw.hex()})
    field['indicators'] = indicator_raw.decode('ascii', 'backslashreplace')
    field['indicator_positions'] = [{'index': index, 'value': chr(byte),
                                     'position': start + index}
                                    for index, byte in enumerate(indicator_raw)]
    # leader/11 是子字段标识符长度，含 0x1f 定界符本身。
    code_length = identifier_length - 1
    if code_length < 1:
        errors.append({'code': 'subfield_code_length_invalid',
                       'reason': 'leader/11 声明的子字段标识符长度不足',
                       'tag': tag, 'identifier_length': identifier_length})
        code_length = 1
    field['subfields'] = _subfields(data, start + indicator_count,
                                    start + len(payload), code_length, allowed,
                                    lambda error: errors.append(
                                        dict(error, tag=tag, field_start=start)), tag)
    return field



def _export(fields):
    title = None
    contributors = []
    publication = []
    for position, field in enumerate(fields):
        tag = field['tag']
        if tag == TITLE_TAG and title is None:
            values = [sub['value'] for sub in field['subfields']
                      if sub.get('kind') == 'subfield' and sub.get('code') in ('a', 'b')
                      and sub.get('value') is not None]
            title = {'tag': tag, 'field_position': position,
                     'field_start': field['field_start'], 'field_end': field['field_end'],
                     'value': ' '.join(values) if values else None,
                     'subfields': field['subfields']}
        elif tag in CONTRIBUTOR_TAGS:
            names = [sub['value'] for sub in field['subfields']
                     if sub.get('kind') == 'subfield' and sub.get('code') == 'a'
                     and sub.get('value') is not None]
            roles = [sub['value'] for sub in field['subfields']
                     if sub.get('kind') == 'subfield' and sub.get('code') == 'e'
                     and sub.get('value') is not None]
            contributors.append({'tag': tag, 'field_position': position,
                                 'field_start': field['field_start'],
                                 'field_end': field['field_end'],
                                 'indicators': field['indicators'],
                                 'name': ' '.join(names) if names else None,
                                 'roles': roles,
                                 'subfields': field['subfields']})
        elif tag in PUBLICATION_TAGS:
            by_code = {}
            for sub in field['subfields']:
                if sub.get('kind') == 'subfield' and sub.get('value') is not None:
                    by_code.setdefault(sub.get('code'), []).append(sub['value'])
            publication.append({'tag': tag, 'field_position': position,
                                'field_start': field['field_start'],
                                'field_end': field['field_end'],
                                'place': ' '.join(by_code.get('a', [])),
                                'publisher': ' '.join(by_code.get('b', [])),
                                'date': ' '.join(by_code.get('c', [])),
                                'subfields': field['subfields']})
    result = {'title': title, 'contributors': contributors, 'publication': publication}
    if title is not None and title['value'] is not None:
        result['title']['bytes'] = len(title['value'].encode('utf-8'))
        result['title']['characters'] = len(title['value'])
    return result


def _parse_record_body(data, offset, leader, base, index):
    errors = []
    coding = leader['coding_scheme']
    allowed = coding == UTF8_CODING
    if not allowed:
        errors.append({'code': 'unsupported_encoding',
                       'reason': '记录编码 %r（%s）不在本来源契约内，已隔离'
                                 % (coding, leader['encoding']),
                       'coding_scheme': coding})
    entries, entry_length = _parse_directory(data, offset, base, leader, errors)
    fields = []
    for entry in entries:
        if entry.get('absolute_start') is None or entry.get('error'):
            continue
        fields.append(_parse_field(data, entry, leader, allowed, errors))
    control_fields = [field for field in fields if field['kind'] == 'control']
    data_fields = [field for field in fields if field['kind'] == 'data']
    record = {
        'index': index,
        'offset': offset,
        'length': leader['record_length'],
        'base': base,
        'leader': leader,
        'directory': {
            'start': offset + LEADER_LENGTH,
            'end': offset + base - 1,
            'entry_length': entry_length,
            'terminator_position': offset + base - 1,
            'entries': entries,
            'evidence': _evidence(data, offset + LEADER_LENGTH, offset + base - 1),
        },
        'control_fields': control_fields,
        'data_fields': data_fields,
        'fields': fields,
        'identity': _identity(control_fields),
        'export': _export(fields),
        'status': 'unsupported_encoding' if not allowed else ('invalid' if errors else 'ok'),
        'errors': errors,
        'raw': _evidence(data, offset, offset + leader['record_length']),
    }
    return record


def parse_record(data, offset=0, index=0):
    """解析一条外层边界可信的记录；边界不可信时抛出 BoundaryError。"""
    remaining = len(data) - offset
    if remaining < LEADER_LENGTH:
        raise BoundaryError('leader_truncated', '剩余字节不足 24 字节 leader',
                            offset, available=remaining,
                            hex=bytes(data[offset:]).hex())
    leader_raw = bytes(data[offset:offset + LEADER_LENGTH])
    leader = _leader_details(leader_raw)
    declared_length = leader['record_length']
    base = leader['base_address']
    if declared_length is None:
        raise BoundaryError('record_length_invalid', 'leader 00-04 位不是数字',
                            offset, leader_hex=leader_raw.hex())
    if declared_length < MIN_RECORD_LENGTH:
        raise BoundaryError('record_length_too_small',
                            'leader 声明的记录长度小于最小结构长度',
                            offset, declared_length=declared_length,
                            minimum=MIN_RECORD_LENGTH)
    if remaining < declared_length:
        raise BoundaryError(
            'record_truncated', 'leader 声明 %d 字节，实际只剩 %d 字节'
                                % (declared_length, remaining),
            offset, declared_length=declared_length, available=remaining,
            expected_terminator_position=offset + declared_length - 1,
            available_tail=bytes(data[offset:]).hex())
    if data[offset + declared_length - 1] != RECORD_TERMINATOR:
        actual = data[offset + declared_length - 1]
        raise BoundaryError(
            'record_terminator_mismatch',
            'leader 声明长度末字节不是 0x1d',
            offset, declared_length=declared_length,
            expected_terminator_position=offset + declared_length - 1,
            actual_hex='%02x' % actual,
            tail_hex=bytes(data[max(offset, offset + declared_length - 17):
                               offset + declared_length]).hex())
    if base is None:
        base = -1
    return _parse_record_body(data, offset, leader, base, index)


def parse_batch(data):
    """从头按声明长度切分记录；单条结构错误可越过，边界不可信则停止。"""
    data = bytes(data)
    records = []
    issues = []
    stopped = None
    offset = 0
    while offset < len(data):
        try:
            record = parse_record(data, offset, len(records))
        except BoundaryError as error:
            stopped = error.as_dict()
            break
        records.append(record)
        if record['status'] != 'ok':
            issues.append({'index': record['index'], 'offset': record['offset'],
                           'length': record['length'], 'status': record['status'],
                           'errors': record['errors']})
        offset += record['length']
    complete = stopped is None and offset == len(data)
    return {
        'bytes': len(data),
        'complete': complete,
        'consumed': offset,
        'remaining': len(data) - offset,
        'records': records,
        'issues': issues,
        'stopped': stopped,
    }


def export_records(batch):
    """仅返回可导出的记录及其到字段位置的引用。"""
    exported = []
    for record in batch['records']:
        if record['status'] != 'ok':
            continue
        exported.append({'index': record['index'], 'offset': record['offset'],
                         'length': record['length'], 'identity': record['identity'],
                         'export': record['export'], 'raw': record['raw']})
    return exported


def to_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
