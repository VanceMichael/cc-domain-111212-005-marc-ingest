"""原始交付与书目身份的本地留存。

三层身份分开：

* 交付批次：``(source, sha256(整批原始字节))``。同一来源重复导入同一批
  字节得到同一条收件记录，不会重复建立修订。
* 书目身份：``(source, 003 机构代码, 001 控制号)``。001 只在来源和机构
  的上下文中才有意义；不同馆的相同 001 不会互相覆盖。
* 原件修订：记录本身的 ``sha256``。同一书目身份下出现相同摘要即“原件
  重送”，出现新摘要即“同控制号的新内容”，两者都保留原始字节与解析结果。

没有 001 的记录无法与馆藏书目关联，单独标为 ``unassigned``，不写修订。
结构损坏或字符集不在契约内的记录同样不写修订，但解析证据保留在交付明细
中；整批原始字节也在 ``deliveries.raw`` 中留存。
"""

import hashlib
import json
import sqlite3

from marc import BoundaryError

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  digest TEXT NOT NULL,
  raw BLOB NOT NULL,
  byte_length INTEGER NOT NULL,
  record_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  complete INTEGER NOT NULL,
  consumed INTEGER NOT NULL,
  remaining INTEGER NOT NULL,
  stop_json TEXT,
  UNIQUE(source,digest));
CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  agency TEXT NOT NULL,
  control TEXT NOT NULL,
  identity_basis TEXT NOT NULL,
  UNIQUE(source,agency,control));
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL REFERENCES records(id),
  digest TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  raw BLOB NOT NULL,
  parsed_json TEXT NOT NULL,
  UNIQUE(record_id,digest));
CREATE TABLE IF NOT EXISTS delivery_records(
  delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
  record_index INTEGER NOT NULL,
  offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  record_digest TEXT,
  record_id INTEGER REFERENCES records(id),
  revision_id INTEGER REFERENCES revisions(id),
  outcome TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  PRIMARY KEY(delivery_id,record_index));
"""

OUTCOME_LABELS = {
    'new': '书目身份首次出现',
    'revision': '同一 001/003/来源下的新内容',
    'resend': '同一 001/003/来源下原件重送',
    'invalid': '结构损坏，未写入修订',
    'unsupported_encoding': '字符集不在来源契约内，已隔离',
    'unassigned': '缺少 001，无法关联书目身份',
}


def identity_of(record):
    identity = record['identity']
    return identity['control_001'], identity['agency_003'], identity['basis']


class Archive:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript(SCHEMA)

    def import_batch(self, source, raw, batch):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        if not batch['complete']:
            stopped = batch['stopped'] or {}
            raise BoundaryError(
                stopped.get('code', 'batch_incomplete'),
                '外层边界不可信，整批不写入：' + stopped.get('reason', '批次未闭合'),
                stopped.get('offset', 0),
                **{key: value for key, value in stopped.items()
                   if key not in ('code', 'reason', 'offset', 'trusted')})
        if not batch['records']:
            raise ValueError('交付批次不含记录，未建立收件身份')
        raw = bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        existing = self.connection.execute(
            'SELECT id FROM deliveries WHERE source=? AND digest=?',
            (source, digest)).fetchone()
        if existing is not None:
            return self.delivery(existing[0], duplicate=True)
        with self.connection:
            delivery_id = self._insert_delivery(source, digest, raw, batch)
            for record in batch['records']:
                self._insert_record(delivery_id, source, raw, record)
        return self.delivery(delivery_id, duplicate=False)

    def _insert_delivery(self, source, digest, raw, batch):
        stopped = batch['stopped']
        cursor = self.connection.execute(
            'INSERT INTO deliveries(source,digest,raw,byte_length,record_count,status,'
            'complete,consumed,remaining,stop_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (source, digest, raw, len(raw), len(batch['records']),
             'complete' if batch['complete'] else 'halted',
             int(batch['complete']), batch['consumed'], batch['remaining'],
             json.dumps(stopped, ensure_ascii=False) if stopped else None))
        return cursor.lastrowid

    def _insert_record(self, delivery_id, source, raw, record):
        status = record['status']
        control, agency, basis = identity_of(record)
        record_id = revision_id = None
        record_digest = None
        if status == 'ok':
            record_digest = hashlib.sha256(raw[record['offset']:
                                              record['offset'] + record['length']]).hexdigest()
            if control is None:
                outcome = 'unassigned'
            else:
                record_id, created = self._record_id(source, agency, control, basis)
                revision_id, fresh = self._revision_id(record_id, record_digest, raw, record)
                if fresh:
                    outcome = 'new' if created else 'revision'
                else:
                    outcome = 'resend'
        else:
            outcome = status
        detail = {
            'leader': record['leader'],
            'directory': record['directory'],
            'identity': record['identity'],
            'export': record['export'],
            'errors': record['errors'],
        }
        self.connection.execute(
            'INSERT INTO delivery_records(delivery_id,record_index,offset,length,'
            'record_digest,record_id,revision_id,outcome,status,detail_json) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (delivery_id, record['index'], record['offset'], record['length'],
             record_digest, record_id, revision_id, outcome, status,
             json.dumps(detail, ensure_ascii=False, sort_keys=True)))
        return outcome

    def _record_id(self, source, agency, control, basis):
        agency = agency or ''
        row = self.connection.execute(
            'SELECT id FROM records WHERE source=? AND agency=? AND control=?',
            (source, agency, control)).fetchone()
        if row is not None:
            return row[0], False
        cursor = self.connection.execute(
            'INSERT INTO records(source,agency,control,identity_basis) VALUES(?,?,?,?)',
            (source, agency, control, basis))
        return cursor.lastrowid, True

    def _revision_id(self, record_id, digest, raw, record):
        row = self.connection.execute(
            'SELECT id FROM revisions WHERE record_id=? AND digest=?',
            (record_id, digest)).fetchone()
        if row is not None:
            return row[0], False
        ordinal = self.connection.execute(
            'SELECT COALESCE(MAX(ordinal),0)+1 FROM revisions WHERE record_id=?',
            (record_id,)).fetchone()[0]
        cursor = self.connection.execute(
            'INSERT INTO revisions(record_id,digest,ordinal,raw,parsed_json)'
            ' VALUES(?,?,?,?,?)',
            (record_id, digest, ordinal, raw[record['offset']:
                                             record['offset'] + record['length']],
             json.dumps(record, ensure_ascii=False, sort_keys=True)))
        return cursor.lastrowid, True

    def delivery(self, delivery_id, duplicate=False):
        row = self.connection.execute(
            'SELECT id,source,digest,byte_length,record_count,status,complete,'
            'consumed,remaining,stop_json FROM deliveries WHERE id=?',
            (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        records = []
        for item in self.connection.execute(
                'SELECT record_index,offset,length,record_digest,record_id,revision_id,'
                'outcome,status,detail_json FROM delivery_records '
                'WHERE delivery_id=? ORDER BY record_index', (delivery_id,)):
            detail = json.loads(item[8])
            records.append({
                'record_index': item[0], 'offset': item[1], 'length': item[2],
                'record_digest': item[3], 'record_id': item[4],
                'revision_id': item[5], 'outcome': item[6],
                'outcome_explanation': OUTCOME_LABELS.get(item[6], item[6]),
                'status': item[7], 'identity': detail.get('identity'),
                'title': detail.get('export', {}).get('title'),
                'error_codes': [error.get('code') for error in detail.get('errors', [])],
            })
        return {
            'delivery_id': row[0], 'source': row[1], 'digest': row[2],
            'bytes': row[3], 'record_count': row[4], 'status': row[5],
            'complete': bool(row[6]), 'consumed': row[7], 'remaining': row[8],
            'stopped': json.loads(row[9]) if row[9] else None,
            'duplicate_import': duplicate,
            'records': records,
        }

    def list(self):
        return [self.delivery(row[0])
                for row in self.connection.execute('SELECT id FROM deliveries ORDER BY id')]

    def parse_report(self, delivery_id):
        """返回某次交付的完整结构（leader、目录、字段、子字段与证据）。"""
        rows = self.connection.execute(
            'SELECT r.parsed_json FROM delivery_records dr '
            'JOIN revisions r ON r.id=dr.revision_id '
            'WHERE dr.delivery_id=? ORDER BY dr.record_index', (delivery_id,)).fetchall()
        return [json.loads(item[0]) for item in rows]

    def export(self, delivery_id=None):
        """按字段出现位置导出书名、责任者与出版信息。"""
        parameters = []
        clause = ''
        if delivery_id is not None:
            clause = ' WHERE dr.delivery_id=?'
            parameters.append(delivery_id)
        rows = self.connection.execute(
            'SELECT dr.delivery_id,dr.record_index,dr.outcome,r.id,r.digest,r.ordinal,'
            'r.record_id,rec.source,rec.agency,rec.control,rec.identity_basis,'
            'r.parsed_json,r.raw FROM delivery_records dr '
            'JOIN revisions r ON r.id=dr.revision_id '
            'JOIN records rec ON rec.id=r.record_id'
            + clause + ' ORDER BY dr.delivery_id,dr.record_index', parameters)
        result = []
        for row in rows:
            parsed = json.loads(row[11])
            result.append({
                'delivery_id': row[0], 'record_index': row[1], 'outcome': row[2],
                'revision_id': row[3], 'revision_digest': row[4],
                'revision_ordinal': row[5], 'record_id': row[6],
                'identity': {'source': row[7], 'agency_003': row[8] or None,
                             'control_001': row[9], 'basis': row[10]},
                'export': parsed['export'],
                'raw': {'offset': parsed['offset'], 'length': parsed['length'],
                        'sha256': hashlib.sha256(bytes(row[12])).hexdigest()},
            })
        return result

    def raw(self, delivery_id):
        row = self.connection.execute(
            'SELECT raw FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return bytes(row[0])

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
