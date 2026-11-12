def make_record(control, title):
    fields = [('001', control.encode() + b'\x1e'), ('003', b'LOCAL\x1e'),
              ('245', b'10\x1fa' + title.encode('utf8') + b'\x1e'),
              ('700', b'1 \x1faLi\x1feeditor\x1e'), ('700', b'1 \x1faChen\x1feauthor\x1e')]
    directory = b''
    body = b''
    for tag, value in fields:
        directory += tag.encode() + f'{len(value):04d}{len(body):05d}'.encode()
        body += value
    base = 24 + len(directory) + 1
    length = base + len(body) + 1
    leader = f'{length:05d}nam a22{base:05d}   4500'.encode()
    return leader + directory + b'\x1e' + body + b'\x1d'

def samples():
    first = make_record('bk-1', '港口手册')
    return {'sample.mrc': first + make_record('bk-2', 'Safety'),
            'revision.mrc': make_record('bk-1', '新版港口手册'),
            'invalid.mrc': first[:-3]}
