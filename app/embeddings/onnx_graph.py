"""The ONNX graph check: no external data, no operator domain but the standard one.

ADR 0005's Watch item. An ONNX file is a protobuf `ModelProto`. Two of its features
reach past the bytes that `make model` hashed:
- **External data.** A tensor whose `data_location` is EXTERNAL, or that carries
  `external_data` entries, names a file that onnxruntime opens and reads; the path is
  the model's to choose (the onnx path-traversal CVEs of 2024 are this field).
- **Operator domains.** A node outside the standard domain runs a kernel that is not
  part of ONNX: a custom operator library, or one of onnxruntime's own extensions, of
  which `com.microsoft.extensions` calls a remote endpoint.

graph_problems() reads the protobuf wire format with the standard library only, so the
check runs before onnxruntime parses a byte. It walks every place a tensor or a node can
sit: the graph's initializers and sparse initializers, node attributes (a tensor, a
graph, and lists of either), and every subgraph (If, Loop, Scan), to a fixed depth.
Model-local functions and training info are refused outright: the pinned model has
neither, and a closed world needs no second walker. Fields it does not know are
skipped, as protobuf does; a malformed or truncated message is a problem, never a pass.

Messages name structural locations and domains only (truncated), never tensor names
or the external path: the bytes are the model's.

Field numbers: onnx/onnx.proto at v1.23.2 (ADR 0005, Sources).
"""

from __future__ import annotations

from collections.abc import Iterator

__all__ = ("ALLOWED_DOMAINS", "graph_problems")

# The operator set of the ONNX specification: the empty string, or no domain field at
# all (onnx.proto, OperatorSetIdProto.domain). The pinned model uses nothing else.
ALLOWED_DOMAINS = frozenset({""})
# Subgraphs nest through node attributes (If inside Loop inside ...). The pinned model
# has none; the bound keeps a hostile file from exhausting the stack.
MAX_DEPTH = 16
_SHOWN = 40  # characters of a domain shown in a message

# Wire types (protobuf encoding): 0 varint, 1 fixed64, 2 length-delimited, 5 fixed32.
# 3 and 4 are the deprecated groups, which ONNX never uses: refused.
_VARINT, _FIXED64, _LEN, _FIXED32 = 0, 1, 2, 5

# ModelProto
_MODEL_GRAPH, _MODEL_OPSET, _MODEL_TRAINING, _MODEL_FUNCTIONS = 7, 8, 20, 25
# OperatorSetIdProto
_OPSET_DOMAIN = 1
# GraphProto
_GRAPH_NODE, _GRAPH_INITIALIZER, _GRAPH_SPARSE = 1, 5, 15
# NodeProto
_NODE_ATTRIBUTE, _NODE_DOMAIN = 5, 7
# AttributeProto: t, g, tensors, graphs, sparse_tensor, sparse_tensors
_ATTR_TENSOR, _ATTR_GRAPH, _ATTR_TENSORS, _ATTR_GRAPHS = 5, 6, 10, 11
_ATTR_SPARSE, _ATTR_SPARSES = 22, 23
# TensorProto
_TENSOR_EXTERNAL_DATA, _TENSOR_DATA_LOCATION = 13, 14
_EXTERNAL = 1  # TensorProto.DataLocation.EXTERNAL
# SparseTensorProto
_SPARSE_VALUES, _SPARSE_INDICES = 1, 2


class _Malformed(Exception):
    pass


def _varint(buf: memoryview, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise _Malformed
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(buf: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    """(field number, wire type, value) per field: an int, or the bytes of a LEN field."""
    pos = 0
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise _Malformed
        if wire == _VARINT:
            value, pos = _varint(buf, pos)
            yield number, wire, value
        elif wire in (_FIXED64, _FIXED32):
            size = 8 if wire == _FIXED64 else 4
            if pos + size > len(buf):
                raise _Malformed
            yield number, wire, int.from_bytes(buf[pos : pos + size], "little")
            pos += size
        elif wire == _LEN:
            size, pos = _varint(buf, pos)
            if pos + size > len(buf):
                raise _Malformed
            yield number, wire, buf[pos : pos + size]
            pos += size
        else:
            raise _Malformed


def _message(wire: int, value: int | memoryview) -> memoryview:
    if wire != _LEN or not isinstance(value, memoryview):
        raise _Malformed
    return value


def _text(wire: int, value: int | memoryview) -> str:
    try:
        return bytes(_message(wire, value)).decode("utf-8")
    except UnicodeDecodeError:
        raise _Malformed from None


def _domain(where: str, domain: str) -> list[str]:
    if domain in ALLOWED_DOMAINS:
        return []
    shown = domain if len(domain) <= _SHOWN else domain[:_SHOWN] + "…"
    return [f"{where}: operator domain {shown!r} is not allowed"]


def _tensor(buf: memoryview, where: str) -> list[str]:
    problems = []
    for number, wire, value in _fields(buf):
        if number == _TENSOR_EXTERNAL_DATA:
            _message(wire, value)
            problems.append(f"{where}: external data")
        elif number == _TENSOR_DATA_LOCATION:
            if wire != _VARINT:
                raise _Malformed
            if value == _EXTERNAL:
                problems.append(f"{where}: data_location EXTERNAL")
    # One line per tensor: a tensor may carry both markers.
    return sorted(set(problems))


def _sparse(buf: memoryview, where: str) -> list[str]:
    problems = []
    for number, wire, value in _fields(buf):
        if number == _SPARSE_VALUES:
            problems += _tensor(_message(wire, value), f"{where}.values")
        elif number == _SPARSE_INDICES:
            problems += _tensor(_message(wire, value), f"{where}.indices")
    return problems


def _attribute(buf: memoryview, where: str, depth: int) -> list[str]:
    problems = []
    counts = {"t": 0, "tensors": 0, "g": 0, "graphs": 0, "sparse": 0}
    for number, wire, value in _fields(buf):
        if number in (_ATTR_TENSOR, _ATTR_TENSORS):
            key = "t" if number == _ATTR_TENSOR else "tensors"
            problems += _tensor(_message(wire, value), f"{where}.{key}[{counts[key]}]")
            counts[key] += 1
        elif number in (_ATTR_GRAPH, _ATTR_GRAPHS):
            key = "g" if number == _ATTR_GRAPH else "graphs"
            sub = f"{where}.{key}[{counts[key]}]"
            problems += _graph(_message(wire, value), sub, depth + 1)
            counts[key] += 1
        elif number in (_ATTR_SPARSE, _ATTR_SPARSES):
            problems += _sparse(_message(wire, value), f"{where}.sparse[{counts['sparse']}]")
            counts["sparse"] += 1
    return problems


def _node(buf: memoryview, where: str, depth: int) -> list[str]:
    problems = []
    domain = ""
    attributes = 0
    for number, wire, value in _fields(buf):
        if number == _NODE_DOMAIN:
            domain = _text(wire, value)
        elif number == _NODE_ATTRIBUTE:
            problems += _attribute(_message(wire, value), f"{where}.attribute[{attributes}]", depth)
            attributes += 1
    return _domain(where, domain) + problems


def _graph(buf: memoryview, where: str, depth: int) -> list[str]:
    if depth > MAX_DEPTH:
        return [f"{where}: subgraphs nested deeper than {MAX_DEPTH}"]
    problems = []
    counts = {"node": 0, "initializer": 0, "sparse_initializer": 0}
    for number, wire, value in _fields(buf):
        if number == _GRAPH_NODE:
            problems += _node(_message(wire, value), f"{where}.node[{counts['node']}]", depth)
            counts["node"] += 1
        elif number == _GRAPH_INITIALIZER:
            at = f"{where}.initializer[{counts['initializer']}]"
            problems += _tensor(_message(wire, value), at)
            counts["initializer"] += 1
        elif number == _GRAPH_SPARSE:
            at = f"{where}.sparse_initializer[{counts['sparse_initializer']}]"
            problems += _sparse(_message(wire, value), at)
            counts["sparse_initializer"] += 1
    return problems


def _model(buf: memoryview) -> list[str]:
    problems = []
    graphs = opsets = 0
    for number, wire, value in _fields(buf):
        if number == _MODEL_GRAPH:
            problems += _graph(_message(wire, value), "graph", 0)
            graphs += 1
        elif number == _MODEL_OPSET:
            for field, kind, item in _fields(_message(wire, value)):
                if field == _OPSET_DOMAIN:
                    problems += _domain(f"opset_import[{opsets}]", _text(kind, item))
            opsets += 1
        elif number == _MODEL_FUNCTIONS:
            _message(wire, value)
            problems.append("model-local functions are refused")
        elif number == _MODEL_TRAINING:
            _message(wire, value)
            problems.append("training info is refused")
    if graphs != 1:
        problems.append(f"{graphs} graphs, expected 1")
    if not opsets:
        problems.append("no opset_import")
    return list(dict.fromkeys(problems))


def graph_problems(model: bytes) -> list[str]:
    """One line per way the serialized ModelProto reaches past its own bytes.

    An empty list means: one graph, no external data anywhere, every node and opset in
    the standard domain, no functions and no training info. A file that is not a
    well-formed protobuf gives one problem.
    """
    if type(model) is not bytes:
        raise TypeError(f"graph_problems takes bytes, not {type(model).__name__}")
    try:
        return _model(memoryview(model))
    except _Malformed:
        return ["not a well-formed ONNX protobuf"]
    except RecursionError:
        return ["not a well-formed ONNX protobuf (too deeply nested)"]
