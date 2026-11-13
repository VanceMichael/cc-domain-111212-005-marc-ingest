"""来源检查：按声明字节边界逐条切分，不扫描猜测。"""

from marc import BoundaryError, parse_batch


def inspect_bytes(data):
    batch = parse_batch(data)
    if not batch['complete']:
        stopped = batch['stopped']
        raise BoundaryError(stopped['code'], stopped['reason'], stopped['offset'],
                            **{key: value for key, value in stopped.items()
                               if key not in ('code', 'reason', 'offset', 'trusted')})
    return [{'offset': record['offset'], 'length': record['length'],
             'base': record['base'], 'encoding': record['leader']['encoding'],
             'status': record['status'],
             'error_codes': [error.get('code') for error in record['errors']]}
            for record in batch['records']]
