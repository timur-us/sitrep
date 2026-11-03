"""The tool registry refuses what the harness must never offer the model (plan PR 6).

Registration reads the arguments model's JSON schema (`model_json_schema()`), recursively
and including `$defs`, and the model's fields with every name they accept:
- every object schema says `additionalProperties: false`, which also refuses
  `dict[str, Any]`; a value without a type (`Any`, a bare `list`) is refused too;
- no accepted name, alias or alias choice matches the identity pattern, and names are
  plain ASCII, so a homoglyph cannot pass the pattern;
- DRAFT and SIDE_EFFECT tools require approval, and READ tools return `Untrusted[...]`;
- effect and data class are members of their enums; names are unique and fit the
  Messages API's tool-name rule.

validate_call() answers with structured errors and never echoes an input value.

Every planted case pairs with the clean spec, which registers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Json,
    create_model,
    field_validator,
)

from app.harness.context import Untrusted
from app.harness.tools import (
    IDENTITY_TERMS,
    CallError,
    DataClass,
    Effect,
    RegistrationError,
    RejectedCall,
    ToolRegistry,
    ToolSpec,
    ValidCall,
    identity_term,
)

FORBID = ConfigDict(extra="forbid")


class Clean(BaseModel):
    model_config = FORBID
    query: str = Field(min_length=1, max_length=50)
    limit: int = Field(default=3, ge=1, le=10)


def clean_spec(**changes: Any) -> ToolSpec:
    base = ToolSpec(
        name="probe_tool",
        description="A tool for the registry tests.",
        arguments=Clean,
        result=Untrusted[str],
        effect=Effect.READ,
        data_class=DataClass.CONFIDENTIAL,
        requires_approval=False,
    )
    return replace(base, **changes)


def problems_of(spec: ToolSpec, registry: ToolRegistry | None = None) -> tuple[str, ...]:
    registry = registry or ToolRegistry()
    with pytest.raises(RegistrationError) as refused:
        registry.register(spec)
    assert spec.name not in registry, "a refused spec was registered anyway"
    return refused.value.problems


def model(name: str, config: ConfigDict | None = FORBID, **fields: Any) -> type[BaseModel]:
    return create_model(name, __config__=config, **fields)


def test_clean_spec_registers() -> None:
    registry = ToolRegistry()
    registry.register(clean_spec())
    assert registry.names() == ("probe_tool",)
    assert registry.spec("probe_tool") == clean_spec()
    assert registry.schema("probe_tool") == Clean.model_json_schema()


# Effects and approval.


@pytest.mark.parametrize("effect", [Effect.DRAFT, Effect.SIDE_EFFECT])
def test_effects_need_approval(effect: Effect) -> None:
    problems = problems_of(clean_spec(effect=effect, result=str))
    assert any("requires_approval" in problem for problem in problems), problems
    ToolRegistry().register(clean_spec(effect=effect, result=str, requires_approval=True))


@pytest.mark.parametrize(
    "result",
    [str, list[str], Untrusted, list[Untrusted[str]], dict[str, Untrusted[str]]],
    ids=["str", "list", "bare-Untrusted", "list-of-Untrusted", "dict-of-Untrusted"],
)
def test_read_tools_return_untrusted(result: Any) -> None:
    problems = problems_of(clean_spec(result=result))
    assert any("Untrusted[" in problem for problem in problems), problems


def test_requires_approval_is_a_bool() -> None:
    problems = problems_of(clean_spec(effect=Effect.DRAFT, result=str, requires_approval=1))
    assert any("requires_approval" in problem for problem in problems), problems


class LookAlike(StrEnum):
    READ = "read"
    CONFIDENTIAL = "confidential"


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"data_class": "confidential"}, "data class"),
        ({"data_class": LookAlike.CONFIDENTIAL}, "data class"),
        ({"data_class": None}, "data class"),
        ({"effect": "read"}, "effect"),
        ({"effect": LookAlike.READ}, "effect"),
    ],
    ids=["dc-str", "dc-lookalike", "dc-none", "effect-str", "effect-lookalike"],
)
def test_unknown_effect_or_data_class(changes: dict[str, Any], needle: str) -> None:
    problems = problems_of(clean_spec(**changes))
    assert any(needle in problem for problem in problems), problems


@pytest.mark.parametrize(
    "name", ["search docs", "", "x" * 65, "tool/1", "werkzeug-ä", 7], ids=lambda n: repr(n)[:12]
)
def test_tool_names_fit_the_api(name: object) -> None:
    problems = problems_of(clean_spec(name=name))
    assert any("tool name" in problem for problem in problems), problems


def test_duplicate_name() -> None:
    registry = ToolRegistry()
    registry.register(clean_spec())
    with pytest.raises(RegistrationError) as refused:
        registry.register(clean_spec(description="Another tool under the same name."))
    assert any("already registered" in problem for problem in refused.value.problems)
    assert registry.spec("probe_tool").description == clean_spec().description


def test_every_problem_is_reported() -> None:
    problems = problems_of(clean_spec(effect=Effect.DRAFT, result=str, data_class="x"))
    assert any("requires_approval" in problem for problem in problems), problems
    assert any("data class" in problem for problem in problems), problems


@dataclass
class NotAModel:
    query: str


@pytest.mark.parametrize("arguments", [NotAModel, Clean(query="x"), dict], ids=str)
def test_arguments_are_a_pydantic_model_class(arguments: object) -> None:
    problems = problems_of(clean_spec(arguments=arguments))
    assert any("BaseModel" in problem for problem in problems), problems


# additionalProperties: false everywhere, and no untyped values.


class Inner(BaseModel):  # default config: extra="ignore"
    term: str


class InnerForbid(BaseModel):
    model_config = FORBID
    term: str


class DefaultConfig(BaseModel):
    query: str


class AllowConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    query: str


class NestedDefault(BaseModel):
    model_config = FORBID
    filters: Inner


class NestedOptional(BaseModel):
    model_config = FORBID
    filters: Inner | None = None


class NestedList(BaseModel):
    model_config = FORBID
    filters: list[Inner] = []


class AnyDict(BaseModel):
    model_config = FORBID
    filters: dict[str, Any]


class TypedDictValues(BaseModel):
    model_config = FORBID
    filters: dict[str, int]


class AnyValue(BaseModel):
    model_config = FORBID
    filters: Any


class AnyList(BaseModel):
    model_config = FORBID
    filters: list[Any]


class BareList(BaseModel):
    model_config = FORBID
    filters: list  # type: ignore[type-arg]


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        (DefaultConfig, "additionalProperties"),
        (AllowConfig, "additionalProperties"),
        (NestedDefault, "$defs.Inner: additionalProperties"),
        (NestedOptional, "$defs.Inner: additionalProperties"),
        (NestedList, "$defs.Inner: additionalProperties"),
        (AnyDict, "properties.filters: additionalProperties"),
        (TypedDictValues, "properties.filters: additionalProperties"),
        (AnyValue, "properties.filters: no type"),
        (AnyList, "properties.filters.items: no type"),
        (BareList, "properties.filters.items: no type"),
    ],
    ids=lambda value: getattr(value, "__name__", None) or "",
)
def test_open_schemas_are_refused(arguments: type[BaseModel], needle: str) -> None:
    problems = problems_of(clean_spec(arguments=arguments))
    assert any(needle in problem for problem in problems), problems


def test_nested_models_with_forbid_register() -> None:
    nested = model("Nested", filters=(InnerForbid, ...), more=(list[InnerForbid], []))
    ToolRegistry().register(clean_spec(arguments=nested))


# No identity in arguments.


IDENTITY_NAMES = [
    "advisor_id",
    "advisorId",
    "AdvisorID",
    "advisorid",
    "advisor-id",
    "berater_id",
    "beraterId",
    "benutzer",
    "user",
    "userName",
    "principal",
    "tenant_id",
    "owner",
    "identity",
    "role",
    "on_behalf",
    "onBehalfOf",
]


@pytest.mark.parametrize("name", IDENTITY_NAMES)
def test_identity_field_names_are_refused(name: str) -> None:
    problems = problems_of(clean_spec(arguments=model("Forged", query=(str, ...), **{name: str})))
    assert any(f"{name!r}" in problem and "identity" in problem for problem in problems), problems


def test_identity_through_an_alias() -> None:
    aliased = model("Aliased", query=(str, ...), mandate=(str, Field(alias="advisor_id")))
    problems = problems_of(clean_spec(arguments=aliased))
    assert any("'advisor_id'" in problem for problem in problems), problems


def test_identity_through_an_alias_choice() -> None:
    # The schema shows only the first choice; validation accepts every one.
    choices = model(
        "Choices", query=(str, Field(validation_alias=AliasChoices("query", "advisor_id")))
    )
    assert "advisor_id" not in str(choices.model_json_schema())
    problems = problems_of(clean_spec(arguments=choices))
    assert any("'advisor_id'" in problem for problem in problems), problems


def test_identity_through_a_name_behind_an_alias() -> None:
    # validate_by_name: the field name is accepted as well as the alias.
    by_name = model(
        "ByName",
        ConfigDict(extra="forbid", validate_by_name=True),
        advisor_id=(str, Field(alias="query")),
    )
    problems = problems_of(clean_spec(arguments=by_name))
    assert any("'advisor_id'" in problem for problem in problems), problems


def test_identity_in_a_nested_model() -> None:
    filters = model("Filters", advisor_id=(str, ...))
    problems = problems_of(clean_spec(arguments=model("Outer", filters=(filters, ...))))
    assert any("filters" in problem or "Filters" in problem for problem in problems), problems
    assert any("'advisor_id'" in problem for problem in problems), problems


@pytest.mark.parametrize(
    "name",
    ["\u0430dvisor_id", "query\u200b", "ｑｕｅｒｙ", "anfrage_ä"],
    ids=["cyr", "zw", "fw", "umlaut"],
)
def test_field_names_are_plain_ascii(name: str) -> None:
    problems = problems_of(clean_spec(arguments=model("Odd", **{name: (str, ...)})))
    assert any("ASCII" in problem for problem in problems), problems


def test_identity_terms_are_the_plans() -> None:
    assert set(IDENTITY_TERMS) == {
        "advisor",
        "user",
        "principal",
        "tenant",
        "owner",
        "identity",
        "role",
        "on_behalf",
        "berater",
        "benutzer",
    }


@pytest.mark.parametrize("name", IDENTITY_NAMES + ["ＡＤＶＩＳＯＲ_ID", "on-behalf-of"])
def test_identity_term_matches(name: str) -> None:
    assert identity_term(name) is not None, name


@pytest.mark.parametrize(
    "name", ["query", "mandate_id", "limit", "to", "subject", "body", "title", "due", "notes"]
)
def test_identity_term_passes_plain_names(name: str) -> None:
    assert identity_term(name) is None, name


# validate_call: structured errors, no input echoed.


CANARY = "cnry-7f3a91"


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(clean_spec())
    return registry


def rejected(outcome: ValidCall | RejectedCall) -> RejectedCall:
    assert isinstance(outcome, RejectedCall), f"accepted: {outcome}"
    assert outcome.errors, "rejected without an error"
    for error in outcome.errors:
        assert isinstance(error, CallError), error
        assert type(error.type) is str and type(error.msg) is str and type(error.loc) is tuple
    return outcome


def test_valid_call(registry: ToolRegistry) -> None:
    outcome = registry.validate_call("probe_tool", {"query": "bonds"})
    assert isinstance(outcome, ValidCall), outcome
    assert outcome.spec.name == "probe_tool"
    assert outcome.arguments == Clean(query="bonds", limit=3)


def test_unknown_tool(registry: ToolRegistry) -> None:
    outcome = rejected(registry.validate_call(f"tool_{CANARY}", {"query": "x"}))
    assert outcome.tool is None
    assert [error.type for error in outcome.errors] == ["unknown_tool"]
    assert CANARY not in repr(outcome)


@pytest.mark.parametrize(
    ("arguments", "kind"),
    [
        ({"query": "x", "extra": CANARY}, "extra_forbidden"),
        ({"query": "x", "limit": CANARY}, "int_parsing"),
        ({"query": "x" * 51 + CANARY}, "string_too_long"),
        ({}, "missing"),
        ([CANARY], "model_type"),
        (CANARY, "model_type"),
        (None, "model_type"),
    ],
    ids=["extra", "type", "length", "missing", "list", "str", "none"],
)
def test_errors_are_structured_and_echo_no_input(
    registry: ToolRegistry, arguments: object, kind: str
) -> None:
    outcome = rejected(registry.validate_call("probe_tool", arguments))
    assert outcome.tool == "probe_tool"
    assert kind in [error.type for error in outcome.errors], outcome.errors
    assert CANARY not in repr(outcome.errors), outcome.errors


@pytest.mark.parametrize(
    ("arguments", "loc"),
    [
        ({"query": "x", "advisor_id": CANARY}, ("advisor_id",)),
        ({"query": "x", "advisorId": CANARY}, ("advisorId",)),
        ({"query": "x", "filters": {"advisor_id": CANARY}}, ("filters", "advisor_id")),
        ({"query": "x", "filters": [{"owner": CANARY}]}, ("filters", 0, "owner")),
        ({"query": "x", "ＢＥＲＡＴＥＲ": CANARY}, ("ＢＥＲＡＴＥＲ",)),
    ],
    ids=["plain", "camel", "nested", "in-list", "fullwidth"],
)
def test_identity_arguments_are_named(
    registry: ToolRegistry, arguments: dict[str, Any], loc: tuple[str | int, ...]
) -> None:
    outcome = rejected(registry.validate_call("probe_tool", arguments))
    assert ("identity_argument", loc) in [(error.type, error.loc) for error in outcome.errors]
    assert CANARY not in repr(outcome.errors), outcome.errors


def test_deep_arguments_do_not_crash(registry: ToolRegistry) -> None:
    deep: dict[str, Any] = {"advisor_id": CANARY}
    for _ in range(5000):
        deep = {"x": deep}
    outcome = rejected(registry.validate_call("probe_tool", {"query": "x", "filters": deep}))
    assert "identity_argument" in [error.type for error in outcome.errors]


# Review findings of PR 6 (sec-1, sec-3, test-1 to test-7).


class JsonDict(BaseModel):
    model_config = FORBID
    query: str
    filters: Json[dict[str, str]]


class JsonModel(BaseModel):
    model_config = FORBID
    query: str
    filters: Json[InnerForbid]


@pytest.mark.parametrize("arguments", [JsonDict, JsonModel], ids=["dict", "model"])
def test_json_string_fields_are_refused(arguments: type[BaseModel]) -> None:
    # sec-1: Json[...] is a string in the schema, with the object inside contentSchema,
    # and a key inside a JSON string is invisible to the identity scan of a call.
    problems = problems_of(clean_spec(arguments=arguments))
    assert any("properties.filters: a JSON-in-a-string" in problem for problem in problems), (
        problems
    )


def test_identity_through_an_alias_path() -> None:
    # test-1: AliasPath("advisor_id", 0) reads arguments["advisor_id"][0].
    path = model("Path", query=(str, Field(validation_alias=AliasPath("advisor_id", 0))))
    problems = problems_of(clean_spec(arguments=path))
    assert any("'advisor_id'" in problem for problem in problems), problems


HIDDEN = model("Hidden", query=(str, Field(validation_alias=AliasChoices("query", "advisor_id"))))


@pytest.mark.parametrize(
    "annotation",
    [list[HIDDEN], HIDDEN | None, tuple[HIDDEN, ...], Annotated[HIDDEN, Field(description="x")]],
    ids=["list", "optional", "tuple", "annotated"],
)
def test_identity_alias_choice_in_a_nested_container(annotation: Any) -> None:
    # test-2: an alias choice the schema does not show, inside a container.
    problems = problems_of(clean_spec(arguments=model("Outer", items=(annotation, ...))))
    assert any("Hidden.query" in problem and "'advisor_id'" in problem for problem in problems), (
        problems
    )


def test_empty_description() -> None:
    # test-3
    for description in ("", "   ", None):
        problems = problems_of(clean_spec(description=description))
        assert "description: empty" in problems, problems


class Node(BaseModel):
    model_config = FORBID
    name: str = Field(max_length=20)
    children: tuple[Node, ...] = ()


def test_a_recursive_model_registers() -> None:
    # test-3: the walks end on a model that refers to itself.
    ToolRegistry().register(clean_spec(arguments=Node))


def test_schema_is_a_fresh_copy() -> None:
    # test-4: a caller that changes the schema it got changes nothing in the registry.
    registry = ToolRegistry()
    registry.register(clean_spec())
    first = registry.schema("probe_tool")
    first["additionalProperties"] = True
    first["properties"]["advisor_id"] = {"type": "string"}
    assert registry.schema("probe_tool") == Clean.model_json_schema()


@pytest.mark.parametrize("name", ["username", "ownership", "parole", "userId", "co_owner"])
def test_identity_near_misses_are_refused_by_design(name: str) -> None:
    # test-5 / spec-4: the substring match refuses these too; a field gets another name.
    assert identity_term(name) is not None, name


def test_a_canary_in_a_key_name_is_in_the_location(registry: ToolRegistry) -> None:
    # test-6 / sec-3: pinned as documented. A location names the keys the model sent, so
    # traces record error types only (M1); the message never carries them.
    outcome = rejected(registry.validate_call("probe_tool", {"query": "x", CANARY: 1}))
    assert [(error.type, error.loc) for error in outcome.errors] == [("extra_forbidden", (CANARY,))]
    assert all(CANARY not in error.msg for error in outcome.errors)


class Echoing(BaseModel):
    model_config = FORBID
    query: str

    @field_validator("query")
    @classmethod
    def refuse(cls, value: str) -> str:
        raise ValueError(f"not allowed: {value}")


class KindA(BaseModel):
    model_config = FORBID
    kind: Literal["a"]


class KindB(BaseModel):
    model_config = FORBID
    kind: Literal["b"]


class Tagged(BaseModel):
    model_config = FORBID
    item: Annotated[KindA | KindB, Discriminator("kind")]


@pytest.mark.parametrize(
    ("arguments", "call", "kind"),
    [
        (Echoing, {"query": CANARY}, "value_error"),
        (Tagged, {"item": {"kind": CANARY}}, "union_tag_invalid"),
    ],
    ids=["validator", "union-tag"],
)
def test_messages_that_would_echo_the_input_are_replaced(
    arguments: type[BaseModel], call: dict[str, Any], kind: str
) -> None:
    # sec-3 / test-6: pydantic writes the input into these messages.
    registry = ToolRegistry()
    registry.register(clean_spec(arguments=arguments))
    outcome = rejected(registry.validate_call("probe_tool", call))
    assert [error.type for error in outcome.errors] == [kind]
    assert CANARY not in repr(outcome.errors), outcome.errors


@pytest.mark.parametrize(
    ("arguments", "kind"),
    [
        ({"query": "x", "filters": [[[[{"advisor_id": CANARY}]]]]}, "identity_argument"),
        ({"query": "x", 1: "y"}, "invalid_key"),
        ({"query": "x", "advis\u200bor_id": CANARY}, "identity_argument"),
    ],
    ids=["deep-list", "int-key", "zero-width"],
)
def test_odd_argument_shapes(registry: ToolRegistry, arguments: dict[Any, Any], kind: str) -> None:
    # test-7
    outcome = rejected(registry.validate_call("probe_tool", arguments))
    assert kind in [error.type for error in outcome.errors], outcome.errors
