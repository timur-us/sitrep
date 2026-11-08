"""A minimal ONNX protobuf writer, for planted models in the tests.

It writes the wire format by hand, so the tests need no `onnx` package. Field numbers
are onnx/onnx.proto at v1.23.2, as in app/embeddings/onnx_graph.py. A planted model is
small and loadable: test_onnx_graph.py runs the clean one, and the controls, in
onnxruntime itself, so a refusal is the check's and never a broken file's.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

FLOAT, INT64 = 1, 7  # TensorProto.DataType
# AttributeProto.AttributeType
ATTR_TENSOR, ATTR_GRAPH, ATTR_TENSORS, ATTR_GRAPHS = 4, 5, 9, 10
ATTR_SPARSE_TENSOR, ATTR_SPARSE_TENSORS = 11, 12


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number: int, value: int | bytes | str) -> bytes:
    """One field: an int as a varint (wire type 0), bytes or str length-delimited (2)."""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    data = value.encode("utf-8") if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(data)) + data


def entry(key: str, value: str) -> bytes:
    """StringStringEntryProto."""
    return field(1, key) + field(2, value)


def tensor(
    name: str,
    values: Sequence[float] = (),
    *,
    location: str | None = None,
    marked: bool = True,
    listed: bool = True,
) -> bytes:
    """A float tensor of shape [len(values)]: raw data, or external data at location.

    External data can be marked by data_location EXTERNAL (marked) and by an
    external_data entry (listed); a planted tensor may carry one without the other.
    """
    out = field(1, len(values)) + field(2, FLOAT) + field(8, name)
    if location is None:
        return out + field(9, struct.pack(f"<{len(values)}f", *values))
    if listed:
        out += field(13, entry("location", location))
    if marked:
        out += field(14, 1)
    return out


def wide(number: int, value: bytes | str) -> bytes:
    """A length-delimited field whose tag sets bit 32: protobuf's C++ parser, the one
    onnxruntime uses, reads tags as 32 bits and drops it, so the field is still `number`."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return varint((number << 3 | 2) | (1 << 32)) + varint(len(data)) + data


def value_info(name: str, size: int, *, elem_type: int = FLOAT, dims: Sequence[int] = ()) -> bytes:
    """ValueInfoProto: a tensor of elem_type and shape dims, by default float [size]."""
    shape = b"".join(field(1, field(1, dim)) for dim in (dims or (size,)))  # dim, dim_value
    tensor_type = field(1, elem_type) + field(2, shape)  # elem_type, shape
    return field(1, name) + field(2, field(1, tensor_type))  # name, TypeProto.tensor_type


def attribute(name: str, *, kind: int, **values: bytes | Sequence[bytes]) -> bytes:
    """AttributeProto with t, g, tensors, graphs or sparse tensors; kind is its type."""
    numbers = {
        "t": 5,
        "g": 6,
        "tensors": 10,
        "graphs": 11,
        "sparse_tensor": 22,
        "sparse_tensors": 23,
    }
    out = field(1, name) + field(20, kind)
    for key, value in values.items():
        items = [value] if isinstance(value, bytes) else list(value)
        out += b"".join(field(numbers[key], item) for item in items)
    return out


def int_attribute(name: str, value: int) -> bytes:
    """AttributeProto of type INT (i)."""
    return field(1, name) + field(20, 2) + field(3, value)


def node(
    op: str,
    inputs: Sequence[str],
    outputs: Sequence[str],
    *,
    domain: str | None = None,
    attributes: Sequence[bytes] = (),
) -> bytes:
    out = b"".join(field(1, name) for name in inputs)
    out += b"".join(field(2, name) for name in outputs)
    out += field(4, op)
    out += b"".join(field(5, item) for item in attributes)
    if domain is not None:
        out += field(7, domain)
    return out


def sparse(values: bytes, indices: bytes) -> bytes:
    """SparseTensorProto."""
    return field(1, values) + field(2, indices) + field(3, 2)


def graph(
    nodes: Sequence[bytes],
    *,
    initializers: Sequence[bytes] = (),
    sparse_initializers: Sequence[bytes] = (),
    inputs: Sequence[bytes] = (),
    outputs: Sequence[bytes] = (),
    name: str = "planted",
) -> bytes:
    out = b"".join(field(1, item) for item in nodes) + field(2, name)
    out += b"".join(field(5, item) for item in initializers)
    out += b"".join(field(11, item) for item in inputs)
    out += b"".join(field(12, item) for item in outputs)
    out += b"".join(field(15, item) for item in sparse_initializers)
    return out


def model(
    body: bytes,
    *,
    opsets: Sequence[tuple[str | None, int]] = ((None, 17),),
    extra: bytes = b"",
) -> bytes:
    """ModelProto at IR version 8. An opset domain of None leaves the field out."""
    out = field(1, 8) + field(2, "planted")
    for domain, version in opsets:
        opset = (b"" if domain is None else field(1, domain)) + field(2, version)
        out += field(8, opset)
    return out + field(7, body) + extra


def add_model(
    *, weights: bytes | None = None, opsets: Sequence[tuple[str | None, int]] = ((None, 17),)
) -> bytes:
    """Y = X + W for float[2]: a model onnxruntime loads and runs.

    weights is W's TensorProto; by default the raw values (1, 2)."""
    w = tensor("W", (1.0, 2.0)) if weights is None else weights
    body = graph(
        [node("Add", ["X", "W"], ["Y"])],
        initializers=[w],
        inputs=[value_info("X", 2)],
        outputs=[value_info("Y", 2)],
    )
    return model(body, opsets=opsets)


def unary_model(op: str, *, domain: str | None, opsets: Sequence[tuple[str | None, int]]) -> bytes:
    """Y = op(X) for float[2], the node in domain."""
    body = graph(
        [node(op, ["X"], ["Y"], domain=domain)],
        inputs=[value_info("X", 2)],
        outputs=[value_info("Y", 2)],
    )
    return model(body, opsets=opsets)


def encoder_model(
    *, output: str = "last_hidden_state", elem_type: int = INT64, width: int = 384
) -> bytes:
    """Shaped like the embedder's model on the outside, and nothing inside: input_ids and
    attention_mask of elem_type (unused), one float output [1, 2, width] named output."""
    dims = (1, 2, width)
    zeros = (
        b"".join(field(1, dim) for dim in dims)
        + field(2, FLOAT)
        + field(8, "H")
        + field(9, bytes(4 * 2 * width))
    )
    ids = value_info("input_ids", 0, elem_type=elem_type, dims=(1, 2))
    mask = value_info("attention_mask", 0, elem_type=elem_type, dims=(1, 2))
    body = graph(
        [node("Identity", ["H"], [output])],
        initializers=[zeros],
        inputs=[ids, mask],
        outputs=[value_info(output, 0, dims=dims)],
    )
    return model(body)
