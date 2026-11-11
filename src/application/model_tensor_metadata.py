from pathlib import Path


def tensor_metadata(path):
    def varint(stream, end):
        value = 0
        for shift in range(0, 70, 7):
            if stream.tell() >= end:
                break
            byte = stream.read(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("模型元数据无效")

    def fields(stream, end):
        while stream.tell() < end:
            tag = varint(stream, end)
            kind = tag & 7
            if kind == 0:
                yield tag >> 3, kind, varint(stream, end)
            elif kind in (1, 2, 5):
                count = varint(stream, end) if kind == 2 else 8 if kind == 1 else 4
                limit = stream.tell() + count
                if limit > end:
                    raise ValueError("模型元数据越界")
                yield tag >> 3, kind, limit
                stream.seek(limit)
            else:
                raise ValueError("模型元数据类型无效")

    def shape(stream, end):
        result = []
        for number, kind, value in fields(stream, end):
            if number == 1 and kind == 2:
                dimension = 0
                for field, wire, item in fields(stream, value):
                    if field == 1 and wire == 0:
                        dimension = item
                result.append(dimension)
        return tuple(result)

    def tensor(stream, end):
        dtype, dimensions = None, ()
        for number, kind, value in fields(stream, end):
            if number == 1 and kind == 0:
                dtype = value
            elif number == 2 and kind == 2:
                dimensions = shape(stream, value)
        if dtype != 1 or len(dimensions) != 4:
            raise ValueError("模型需要四维 float32 张量")
        return dimensions

    def info(stream, end):
        for number, kind, value in fields(stream, end):
            if number == 2 and kind == 2:
                for field, wire, item in fields(stream, value):
                    if field == 1 and wire == 2:
                        return tensor(stream, item)
        raise ValueError("模型张量类型无效")

    inputs, outputs = [], []
    path = Path(path)
    with path.open("rb") as stream:
        for number, kind, end in fields(stream, path.stat().st_size):
            if number != 7 or kind != 2:
                continue
            for field, wire, limit in fields(stream, end):
                if field in (11, 12) and wire == 2:
                    (inputs if field == 11 else outputs).append(info(stream, limit))
                elif field == 5 and wire == 2:
                    for key, form, item in fields(stream, limit):
                        if key == 13 or key == 14 and form == 0 and item:
                            raise ValueError("模型外部权重未登记")
    if len(inputs) != 1 or not 1 <= len(outputs) <= 32:
        raise ValueError("模型输入输出数量无效")
    return inputs[0], tuple(outputs)
