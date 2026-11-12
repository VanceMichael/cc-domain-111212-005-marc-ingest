def inspect_bytes(data):
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 24:
            raise ValueError('记录 leader 截断')
        leader = data[offset:offset + 24]
        length = int(leader[:5])
        base = int(leader[12:17])
        if length < 25 or base < 25 or base > length or offset + length > len(data):
            raise ValueError('记录长度或字段起点越界')
        if data[offset + length - 1] != 29:
            raise ValueError('记录缺少结束符')
        result.append({'offset': offset, 'length': length, 'base': base,
                       'encoding': chr(leader[9])})
        offset += length
    return result
