"""The ONNX graph check refuses external data and operator domains (ADR 0005, Watch).

Every planted model is written by onnx_proto.py and is a real ONNX file: the clean one
loads and runs in onnxruntime, and so do the controls that show what the check is for.
Loaded from a path, onnxruntime reads a tensor's external file; it runs a node of its
own `com.microsoft` domain. graph_problems() refuses both from the bytes alone.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import onnx_proto as p
import pytest

from app.embeddings.onnx_graph import ALLOWED_DOMAINS, MAX_DEPTH, graph_problems

X = np.array([1.0, 1.0], dtype=np.float32)
CANARY = "cnry-ext-7d1f"


def run(model: bytes | str) -> list[float]:
    import onnxruntime as ort

    session = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    return session.run(None, {"X": X})[0].tolist()


def test_the_standard_domain_only() -> None:
    assert ALLOWED_DOMAINS == frozenset({""})


def test_clean_model_passes_and_runs() -> None:
    model = p.add_model()
    assert graph_problems(model) == []
    assert run(model) == [2.0, 3.0]
    # The domain field written out as "" is the same standard domain.
    assert graph_problems(p.add_model(opsets=[("", 17)])) == []


def test_external_initializer_is_refused(tmp_path: Path) -> None:
    model = p.add_model(weights=p.tensor("W", (0.0, 0.0), location="weights.bin"))
    assert graph_problems(model) == [
        "graph.initializer[0]: data_location EXTERNAL",
        "graph.initializer[0]: external data",
    ]
    # Control: loaded from a path, onnxruntime opens the file the model names.
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "weights.bin").write_bytes(struct.pack("<2f", 5.0, 6.0))
    assert run(str(tmp_path / "model.onnx")) == [6.0, 7.0]


@pytest.mark.parametrize(
    ("marked", "listed", "expected"),
    [
        (True, False, ["graph.initializer[0]: data_location EXTERNAL"]),
        (False, True, ["graph.initializer[0]: external data"]),
    ],
)
def test_either_marker_alone_is_refused(marked: bool, listed: bool, expected: list[str]) -> None:
    weights = p.tensor("W", (0.0, 0.0), location="w.bin", marked=marked, listed=listed)
    assert graph_problems(p.add_model(weights=weights)) == expected


def test_external_data_anywhere_is_refused() -> None:
    external = p.tensor("T", (0.0,), location="t.bin")
    clean = p.tensor("C", (1.0,))
    constant = p.node(
        "Constant", [], ["K"], attributes=[p.attribute("value", kind=p.ATTR_TENSOR, t=external)]
    )
    listed = p.node(
        "Planted",
        [],
        ["L"],
        attributes=[p.attribute("values", kind=p.ATTR_TENSORS, tensors=[clean, external])],
    )
    sub = p.graph([p.node("Identity", ["X"], ["Z"])], initializers=[external])
    branch = p.node(
        "If",
        ["B"],
        ["Y"],
        attributes=[
            p.attribute("then_branch", kind=p.ATTR_GRAPH, g=sub),
            p.attribute("else_branch", kind=p.ATTR_GRAPHS, graphs=[sub, sub]),
        ],
    )
    body = p.graph(
        [constant, listed, branch],
        sparse_initializers=[p.sparse(external, clean), p.sparse(clean, external)],
    )
    assert graph_problems(p.model(body)) == [
        "graph.node[0].attribute[0].t[0]: data_location EXTERNAL",
        "graph.node[0].attribute[0].t[0]: external data",
        "graph.node[1].attribute[0].tensors[1]: data_location EXTERNAL",
        "graph.node[1].attribute[0].tensors[1]: external data",
        "graph.node[2].attribute[0].g[0].initializer[0]: data_location EXTERNAL",
        "graph.node[2].attribute[0].g[0].initializer[0]: external data",
        "graph.node[2].attribute[1].graphs[0].initializer[0]: data_location EXTERNAL",
        "graph.node[2].attribute[1].graphs[0].initializer[0]: external data",
        "graph.node[2].attribute[1].graphs[1].initializer[0]: data_location EXTERNAL",
        "graph.node[2].attribute[1].graphs[1].initializer[0]: external data",
        "graph.sparse_initializer[0].values: data_location EXTERNAL",
        "graph.sparse_initializer[0].values: external data",
        "graph.sparse_initializer[1].indices: data_location EXTERNAL",
        "graph.sparse_initializer[1].indices: external data",
    ]


def test_the_external_path_is_never_shown() -> None:
    weights = p.tensor("W", (0.0, 0.0), location=f"../../{CANARY}/weights.bin")
    problems = graph_problems(p.add_model(weights=weights))
    assert problems and not any(CANARY in problem for problem in problems), problems


def test_custom_domain_is_refused_though_onnxruntime_runs_it() -> None:
    model = p.unary_model("Gelu", domain="com.microsoft", opsets=[(None, 17), ("com.microsoft", 1)])
    assert graph_problems(model) == [
        "opset_import[1]: operator domain 'com.microsoft' is not allowed",
        "graph.node[0]: operator domain 'com.microsoft' is not allowed",
    ]
    # Control: onnxruntime runs its own contrib operator.
    assert run(model) == pytest.approx([0.8413447, 0.8413447], abs=1e-6)


@pytest.mark.parametrize(
    "domain", ["com.microsoft.extensions", "ai.onnx.ml", "ai.onnx", "custom.example", " "]
)
def test_every_other_domain_is_refused(domain: str) -> None:
    on_node = p.unary_model("Op", domain=domain, opsets=[(None, 17)])
    assert graph_problems(on_node) == [f"graph.node[0]: operator domain {domain!r} is not allowed"]
    on_opset = p.add_model(opsets=[(None, 17), (domain, 1)])
    assert graph_problems(on_opset) == [
        f"opset_import[1]: operator domain {domain!r} is not allowed"
    ]


def test_domains_in_subgraphs_are_refused() -> None:
    inner = p.graph([p.node("Op", ["X"], ["Y"], domain="custom.example")])
    loop = p.node(
        "Loop", ["M"], ["Y"], attributes=[p.attribute("body", kind=p.ATTR_GRAPH, g=inner)]
    )
    assert graph_problems(p.model(p.graph([loop]))) == [
        "graph.node[0].attribute[0].g[0].node[0]: operator domain 'custom.example' is not allowed"
    ]


def test_a_long_domain_is_shortened() -> None:
    domain = "x" * 200
    (problem,) = graph_problems(p.unary_model("Op", domain=domain, opsets=[(None, 17)]))
    assert problem == f"graph.node[0]: operator domain {'x' * 40 + '…'!r} is not allowed"


def test_functions_and_training_info_are_refused() -> None:
    function = p.field(1, "f") + p.field(10, "") + p.field(7, p.node("Identity", ["a"], ["b"]))
    training = p.field(1, p.graph([p.node("Identity", ["a"], ["b"])]))
    model = p.model(p.graph([p.node("Identity", ["X"], ["Y"])]), extra=p.field(25, function))
    assert graph_problems(model) == ["model-local functions are refused"]
    model = p.model(p.graph([p.node("Identity", ["X"], ["Y"])]), extra=p.field(20, training))
    assert graph_problems(model) == ["training info is refused"]


def nested(depth: int) -> bytes:
    body = p.graph([p.node("Identity", ["X"], ["Y"])])
    for _ in range(depth):
        body = p.graph(
            [p.node("If", ["B"], ["Y"], attributes=[p.attribute("b", kind=p.ATTR_GRAPH, g=body)])]
        )
    return p.model(body)


def test_nesting_is_bounded() -> None:
    assert graph_problems(nested(MAX_DEPTH)) == []
    (problem,) = graph_problems(nested(MAX_DEPTH + 1))
    assert problem.endswith(f"subgraphs nested deeper than {MAX_DEPTH}"), problem


def test_the_model_shape() -> None:
    no_opset = p.model(p.graph([p.node("Identity", ["X"], ["Y"])]), opsets=[])
    assert graph_problems(no_opset) == ["no opset_import"]
    two_graphs = p.add_model() + p.field(7, p.graph([]))
    assert graph_problems(two_graphs) == ["2 graphs, expected 1"]
    assert graph_problems(b"") == ["0 graphs, expected 1", "no opset_import"]


MALFORMED = {
    "truncated": p.add_model()[:-3],
    "a length past the end": p.varint(7 << 3 | 2) + p.varint(1000) + b"x",
    "an endless varint": b"\x08" + b"\xff" * 11,
    "a group": p.varint(7 << 3 | 3),
    "field number zero": p.varint(0 << 3 | 0) + p.varint(1),
    "a domain that is not UTF-8": p.model(p.graph([p.field(4, "Op") + p.field(7, b"\xff\xfe")])),
    "a graph that is a varint": p.field(1, 8) + p.field(7, 3),
    "a data location that is not a varint": p.model(
        p.graph([], initializers=[p.field(8, "W") + p.field(14, b"\x01")])
    ),
    "random bytes": bytes(range(256)) * 4,
    # Review finding sec-1: protobuf's C++ parser reads a tag as 32 bits and drops the
    # rest, so a wider tag is a field the walk would skip and onnxruntime would read.
    "a tag over 32 bits": p.varint((7 << 3 | 2) | (1 << 32)) + p.varint(0),
    "a varint over 64 bits": b"\x08" + b"\xff" * 9 + b"\x02",
}


@pytest.mark.parametrize("data", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_files_are_refused(data: bytes) -> None:
    assert graph_problems(data) == ["not a well-formed ONNX protobuf"]


@pytest.mark.parametrize("data", ["model", bytearray(b"x"), memoryview(b"x"), None])
def test_only_bytes(data: object) -> None:
    with pytest.raises(TypeError):
        graph_problems(data)  # type: ignore[arg-type]


# Review findings sec-1 and sec-2: fields onnxruntime reads that the walk must not skip.
GELU = ((None, 17), ("com.microsoft", 1))


def test_a_domain_behind_a_wide_tag_is_refused() -> None:
    node = p.field(1, "X") + p.field(2, "Y") + p.field(4, "Gelu") + p.wide(7, "com.microsoft")
    opsets = p.field(8, p.field(2, 17)) + p.field(8, p.wide(1, "com.microsoft") + p.field(2, 1))
    body = p.graph([node], inputs=[p.value_info("X", 2)], outputs=[p.value_info("Y", 2)])
    model = p.field(1, 8) + p.field(2, "planted") + opsets + p.field(7, body)
    assert graph_problems(model) == ["not a well-formed ONNX protobuf"]
    # Control: onnxruntime reads both wide tags as the domain fields and runs the node.
    assert run(model) == pytest.approx([0.8413447, 0.8413447], abs=1e-6)


def test_external_data_behind_a_wide_tag_is_refused(tmp_path: Path) -> None:
    hidden = p.wide(13, p.entry("location", "w.bin"))
    weights = p.field(1, 2) + p.field(2, p.FLOAT) + p.field(8, "W") + hidden + p.field(14, 1)
    model = p.add_model(weights=weights)
    assert graph_problems(model) == ["not a well-formed ONNX protobuf"]
    # Control: onnxruntime reads the entry, and from a path it opens the file.
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "w.bin").write_bytes(struct.pack("<2f", 5.0, 6.0))
    assert run(str(tmp_path / "model.onnx")) == [6.0, 7.0]


@pytest.mark.parametrize("location", [2, (1 << 32) + 1, (1 << 63) + 1])
def test_any_data_location_but_default_is_refused(location: int) -> None:
    weights = p.field(1, 2) + p.field(2, p.FLOAT) + p.field(8, "W") + p.field(14, location)
    assert graph_problems(p.add_model(weights=weights)) == [
        "graph.initializer[0]: data_location not DEFAULT"
    ]


def test_a_data_location_cast_to_external_is_read(tmp_path: Path) -> None:
    # Control for the test above: protobuf casts the enum to 32 bits, so 2**32 + 1 is
    # EXTERNAL to onnxruntime, which then opens the file.
    weights = (
        p.field(1, 2)
        + p.field(2, p.FLOAT)
        + p.field(8, "W")
        + p.field(13, p.entry("location", "w.bin"))
        + p.field(14, (1 << 32) + 1)
    )
    model = p.add_model(weights=weights)
    assert graph_problems(model) == [
        "graph.initializer[0]: data_location not DEFAULT",
        "graph.initializer[0]: external data",
    ]
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "w.bin").write_bytes(struct.pack("<2f", 5.0, 6.0))
    assert run(str(tmp_path / "model.onnx")) == [6.0, 7.0]


def test_a_config_in_the_metadata_is_refused() -> None:
    # Review finding sec-3: with ORT_LOAD_CONFIG_FROM_MODEL=1, onnxruntime takes session
    # options from metadata_props["ort_config"] (test_onnx_embedder.py has the control).
    config = p.field(14, p.entry("ort_config", '{"session_options": {"enable_profiling": 1}}'))
    assert graph_problems(p.add_model() + config) == ["metadata ort_config is refused"]
    note = p.field(14, p.entry("note", "planted"))
    assert graph_problems(p.add_model() + note) == []


def test_sparse_attributes_are_walked() -> None:
    # Review finding test-4: a sparse tensor in an attribute, alone and in a list, also
    # inside a subgraph.
    external = p.tensor("T", (0.0,), location="t.bin")
    clean = p.tensor("C", (1.0,))
    one = p.attribute(
        "sparse_value", kind=p.ATTR_SPARSE_TENSOR, sparse_tensor=p.sparse(external, clean)
    )
    many = p.attribute(
        "sparse_values",
        kind=p.ATTR_SPARSE_TENSORS,
        sparse_tensors=[p.sparse(clean, clean), p.sparse(clean, external)],
    )
    inner = p.graph([p.node("Constant", [], ["K"], attributes=[one])])
    branch = p.node("If", ["B"], ["Y"], attributes=[p.attribute("b", kind=p.ATTR_GRAPH, g=inner)])
    body = p.graph([p.node("Constant", [], ["K"], attributes=[one, many]), branch])
    assert graph_problems(p.model(body)) == [
        "graph.node[0].attribute[0].sparse[0].values: data_location EXTERNAL",
        "graph.node[0].attribute[0].sparse[0].values: external data",
        "graph.node[0].attribute[1].sparse[1].indices: data_location EXTERNAL",
        "graph.node[0].attribute[1].sparse[1].indices: external data",
        "graph.node[1].attribute[0].g[0].node[0].attribute[0].sparse[0].values: data_location EXTERNAL",
        "graph.node[1].attribute[0].g[0].node[0].attribute[0].sparse[0].values: external data",
    ]


def outputs(model_path: Path, feeds: dict[str, np.ndarray]) -> list[float]:
    import onnxruntime as ort

    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    return session.run(None, feeds)[0].tolist()


def test_onnxruntime_reads_external_data_where_the_walk_looks(tmp_path: Path) -> None:
    # Review finding test-5: the writer and the walk share their field numbers, so these
    # controls show onnxruntime reading external data from an attribute tensor and from a
    # subgraph's initializer: the fields the walk refuses are the ones onnxruntime uses.
    (tmp_path / "t.bin").write_bytes(struct.pack("<2f", 5.0, 6.0))
    external = p.tensor("T", (0.0, 0.0), location="t.bin")
    constant = p.node(
        "Constant", [], ["K"], attributes=[p.attribute("value", kind=p.ATTR_TENSOR, t=external)]
    )
    model = p.model(p.graph([constant], outputs=[p.value_info("K", 2)]))
    assert graph_problems(model) == [
        "graph.node[0].attribute[0].t[0]: data_location EXTERNAL",
        "graph.node[0].attribute[0].t[0]: external data",
    ]
    (tmp_path / "constant.onnx").write_bytes(model)
    assert outputs(tmp_path / "constant.onnx", {}) == [5.0, 6.0]

    then = p.graph(
        [p.node("Identity", ["T"], ["Z"])], initializers=[external], outputs=[p.value_info("Z", 2)]
    )
    other = p.graph(
        [p.node("Identity", ["C"], ["Z"])],
        initializers=[p.tensor("C", (1.0, 1.0))],
        outputs=[p.value_info("Z", 2)],
    )
    branch = p.node(
        "If",
        ["B"],
        ["Y"],
        attributes=[
            p.attribute("then_branch", kind=p.ATTR_GRAPH, g=then),
            p.attribute("else_branch", kind=p.ATTR_GRAPH, g=other),
        ],
    )
    condition = p.value_info("B", 1, elem_type=9)  # BOOL
    model = p.model(p.graph([branch], inputs=[condition], outputs=[p.value_info("Y", 2)]))
    assert graph_problems(model) == [
        "graph.node[0].attribute[0].g[0].initializer[0]: data_location EXTERNAL",
        "graph.node[0].attribute[0].g[0].initializer[0]: external data",
    ]
    (tmp_path / "branch.onnx").write_bytes(model)
    assert outputs(tmp_path / "branch.onnx", {"B": np.array([True])}) == [5.0, 6.0]
