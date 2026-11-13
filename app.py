"""marc-ingest 命令行入口。"""

import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from marc import BoundaryError, parse_batch
from sampledata import samples
from source import inspect_bytes
from storage import Archive
from transport import ReplaySource, fetch

MAX_INPUT = 16 * 1024 * 1024


def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data


def _summarize(batch):
    return {
        'complete': batch['complete'],
        'bytes': batch['bytes'],
        'consumed': batch['consumed'],
        'remaining': batch['remaining'],
        'records': len(batch['records']),
        'stopped': batch['stopped'],
        'statuses': [record['status'] for record in batch['records']],
    }


def demo():
    """真实启动本地来源，导入同一批次、新内容与原件重送，并展示字节结果。"""
    fixtures = samples()
    routes = {'GET ' + path: [{'base64': base64.b64encode(fixtures[name]).decode()}]
              for path, name in (('/delivery', 'sample.mrc'),
                                 ('/revision', 'revision.mrc'),
                                 ('/resend', 'resend.mrc'),
                                 ('/repeated', 'repeated.mrc'),
                                 ('/truncated', 'truncated-tail.mrc'))}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        fetched = {}
        for key in routes:
            path = key.split(' ', 1)[1]
            status, headers, raw = fetch(server.url + path)
            if status != 200:
                raise ValueError('来源请求失败：' + path)
            fetched[path] = raw
        with Archive(Path(directory) / 'archive.sqlite3') as archive:
            first = archive.import_batch('local-demo', fetched['/delivery'],
                                         parse_batch(fetched['/delivery']))
            repeated = archive.import_batch('local-demo', fetched['/delivery'],
                                            parse_batch(fetched['/delivery']))
            revision = archive.import_batch('local-demo', fetched['/revision'],
                                            parse_batch(fetched['/revision']))
            resend = archive.import_batch('local-demo', fetched['/resend'],
                                          parse_batch(fetched['/resend']))
            deliveries = archive.list()
        repeated_batch = parse_batch(fetched['/repeated'])
        truncated = parse_batch(fetched['/truncated'])
        return {
            'bytes': len(fetched['/delivery']),
            'deliveries': len(deliveries),
            'same_delivery': first['delivery_id'] == repeated['delivery_id'],
            'duplicate_import': repeated['duplicate_import'],
            'requests': len(server.requests),
            'outcomes': {
                'first': [item['outcome'] for item in first['records']],
                'repeated_batch': [item['outcome'] for item in repeated['records']],
                'revision': [item['outcome'] for item in revision['records']],
                'resend': [item['outcome'] for item in resend['records']],
            },
            'title': first['records'][0]['title']['value'],
            'chinese': {'characters': first['records'][0]['title']['characters'],
                        'bytes': first['records'][0]['title']['bytes']},
            'repeated_fields': [item['name'] for item in
                                repeated_batch['records'][0]['export']['contributors']],
            'truncated': {'complete': truncated['complete'],
                          'parsed': [item['identity']['control_001']
                                     for item in truncated['records']],
                          'stopped': truncated['stopped']},
        }


def showcase():
    """演示中文长度、重复字段与截断记录的实际解析结果。"""
    fixtures = samples()
    repeated = parse_batch(fixtures['repeated.mrc'])
    record = repeated['records'][0]
    mixed = parse_batch(fixtures['mixed.mrc'])
    truncated = parse_batch(fixtures['truncated-tail.mrc'])
    invalid = parse_batch(fixtures['invalid.mrc'])
    title = record['export']['title']
    return {
        'chinese_length': {
            'title': title['value'],
            'characters': title['characters'],
            'utf8_bytes': title['bytes'],
            'declared_field_length': title['field_end'] - title['field_start'],
            'subfield_positions': [sub['evidence'] for sub in title['subfields']],
        },
        'repeated_fields': {
            'contributors': [{'tag': item['tag'], 'position': item['field_position'],
                              'name': item['name'], 'roles': item['roles']}
                             for item in record['export']['contributors']],
            'subfield_ordinals': [[sub['ordinal'] for sub in item['subfields']]
                                  for item in record['export']['contributors']],
        },
        'truncated_tail': {
            'complete': truncated['complete'],
            'parsed_before_stop': [item['identity']['control_001']
                                   for item in truncated['records']],
            'stopped': truncated['stopped'],
        },
        'truncated_single': {
            'complete': invalid['complete'],
            'parsed_before_stop': [],
            'stopped': invalid['stopped'],
        },
        'recoverable_middle': {
            'complete': mixed['complete'],
            'statuses': [item['status'] for item in mixed['records']],
            'error_codes': [[error['code'] for error in item['errors']]
                            for item in mixed['records']],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查、字节级结构与原件留存')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture', 'structure', 'export'):
        command = commands.add_parser(name)
        command.add_argument('path', nargs='?' if name == 'export' else None)
        if name in ('capture', 'export'):
            command.add_argument('--database', required=True)
        if name == 'capture':
            command.add_argument('--source', required=True)
        if name == 'export':
            command.add_argument('--delivery', type=int)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')
    commands.add_parser('showcase')
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'showcase':
            result = showcase()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination / name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：' + name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'export':
            with Archive(args.database) as archive:
                result = archive.export(args.delivery)
        elif args.command == 'structure':
            batch = parse_batch(read_input(args.path))
            result = batch
        else:
            raw = read_input(args.path)
            batch = parse_batch(raw)
            if args.command == 'inspect':
                result = inspect_bytes(raw)
            else:
                with Archive(args.database) as archive:
                    result = archive.import_batch(args.source, raw, batch)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        payload = dict(error=type(error).__name__, reason=str(error))
        if isinstance(error, BoundaryError):
            payload.update(error.as_dict())
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
