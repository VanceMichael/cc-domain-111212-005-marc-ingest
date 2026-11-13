"""构造可重复的本地演练夹具（全部为虚构数据）。

夹具覆盖接收时需要区分的情形：中文与多字节长度、重复 700 与重复子字段、
同一 001 在不同 003 机构下、单条内部损坏但外层长度可靠、尾部截断导致边界
不可信、以及非 UTF-8 记录被隔离。
"""

FIELD_TERMINATOR = b'\x1e'
RECORD_TERMINATOR = b'\x1d'
SUBFIELD = b'\x1f'


def record(fields, coding='a'):
    """由 (tag, 不含终止符的字节) 列表构造一条 ISO 2709 记录。"""
    directory = b''
    body = b''
    for tag, payload in fields:
        value = payload + FIELD_TERMINATOR
        directory += tag.encode('ascii') + b'%04d%05d' % (len(value), len(body))
        body += value
    base = 24 + len(directory) + 1
    length = base + len(body) + 1
    leader = b'%05dnam %s22%05d   4500' % (length, coding.encode('ascii'), base)
    assert len(leader) == 24
    return leader + directory + FIELD_TERMINATOR + body + RECORD_TERMINATOR


def data_field(tag, indicators, subfields):
    """subfields 为 (code, value) 序列；重复 code 原样保留。"""
    payload = indicators.encode('ascii')
    for code, value in subfields:
        payload += SUBFIELD + code.encode('ascii') + value.encode('utf-8')
    return tag, payload


def control_field(tag, value):
    return tag, value.encode('utf-8') if isinstance(value, str) else value


def make_record(control, title, agency='LOCAL', creators=None, publication=None,
                subjects=(), coding='a'):
    if creators is None:
        creators = [('Li', ('editor',)), ('Chen', ('author',))]
    fields = [control_field('001', control), control_field('003', agency)]
    fields.append(data_field('245', '10', [('a', title)]))
    if publication:
        place, publisher, date = publication
        fields.append(data_field('260', '  ', [('a', place), ('b', publisher), ('c', date)]))
    for name, roles in creators:
        fields.append(data_field('700', '1 ', [('a', name)] + [('e', role) for role in roles]))
    for subject in subjects:
        fields.append(data_field('650', ' 0', [('a', subject)]))
    return record(fields, coding=coding)


def _first_record():
    return make_record('bk-1', '港口手册')


def _mixed_records():
    first = make_record('bk-10', '完整记录')
    broken = bytearray(make_record('bk-11', '目录入口损坏'))
    # 把第三条目录项（245）的字段长度首位改成非数字：外层 leader 长度与
    # 0x1d 仍然可靠，所以批量可以继续到第三条记录。
    broken[24 + 2 * 12 + 3] = ord('X')
    third = make_record('bk-12', '后续记录')
    return bytes(first) + bytes(broken) + bytes(third)


def _truncated_tail():
    first = make_record('bk-20', '先完整')
    tail = make_record('bk-21', '后截断')
    return first + tail[:-4]


def _repeated():
    fields = [
        control_field('001', 'bk-30'),
        control_field('003', 'LOCAL'),
        data_field('245', '10', [('a', '汉语对照手册'), ('b', '附索引')]),
        data_field('260', '  ', [('a', '北京'), ('b', '联合出版社'), ('c', '2024')]),
        data_field('700', '1 ', [('a', '李'), ('e', '主编'), ('e', '译者')]),
        data_field('700', '1 ', [('a', '陈'), ('e', '著')]),
        data_field('700', '1 ', [('a', '王'), ('e', '校订')]),
        data_field('650', ' 0', [('a', '图书馆学')]),
        data_field('650', ' 0', [('a', '编目')]),
    ]
    return record(fields)


def _agencies():
    return (make_record('shared-1', '甲馆记录', agency='AAA')
            + make_record('shared-1', '乙馆记录', agency='BBB'))


def _latin1():
    fields = [
        control_field('001', 'latin-1'),
        control_field('003', 'LOCAL'),
        ('245', b'10' + SUBFIELD + b'aCaf\xe9'),
    ]
    return record(fields, coding=' ')


def samples():
    sample = _first_record() + make_record('bk-2', 'Safety')
    return {
        'sample.mrc': sample,
        'revision.mrc': make_record('bk-1', '新版港口手册'),
        'invalid.mrc': _first_record()[:-3],
        'resend.mrc': _first_record(),
        'repeated.mrc': _repeated(),
        'agencies.mrc': _agencies(),
        'mixed.mrc': _mixed_records(),
        'truncated-tail.mrc': _truncated_tail(),
        'latin1.mrc': _latin1(),
    }
