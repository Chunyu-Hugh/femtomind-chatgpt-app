"""The compiler's typed encoding, and its inverse.

jamai.compiled_input._typed turns a FitPack conf into a form that keeps what
JSON would lose: int against string keys, bool against int values, floats as
their exact hex. The remote dumper (remote_dump.py) emits that form, so the
candidate's conf crosses the SSH boundary losslessly; untype() turns it back
into the Python values that jamai.compiled_input._differences compares.
"""
import math


def untype(value):
    """The inverse of jamai.compiled_input._typed."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("not a typed value: %r" % (value,))
    kind, payload = value
    if kind == "dict":
        return {untype(k): untype(v) for k, v in payload}
    if kind == "list":
        return [untype(v) for v in payload]
    if kind == "float":
        result = float.fromhex(payload)
        if not math.isfinite(result):
            raise ValueError("nonfinite float")
        return result
    if kind == "int":
        return int(payload)
    if kind == "bool":
        return bool(payload)
    if kind == "str":
        return str(payload)
    if kind == "NoneType":
        return None
    raise ValueError("unknown typed kind %r" % (kind,))


TYPED_SOURCE = r'''
import math
def _typed(value):
    if isinstance(value, dict):
        return ["dict", sorted([[_typed(k), _typed(v)] for k, v in value.items()], key=repr)]
    if isinstance(value, list):
        return ["list", [_typed(v) for v in value]]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite scientific number")
        return ["float", value.hex()]
    if type(value) not in (str, int, bool, type(None)):
        raise ValueError("Unsupported scientific literal")
    return [type(value).__name__, value]
'''
