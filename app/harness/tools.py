"""The tool registry: what the model may call, with declared effects and data classes.

A ToolSpec declares a tool; ToolRegistry.register() admits it only when every rule below
holds, and reports all the rules a spec breaks at once (RegistrationError):
- the name fits the Messages API's tool-name rule and is not taken;
- effect and data class are members of Effect and DataClass, not look-alikes;
- DRAFT and SIDE_EFFECT tools have requires_approval set: the model proposes, the
  advisor approves (app/harness/approval.py);
- READ tools return Untrusted[...] (app/harness/context.py): what a tool reads is data,
  never instructions (CLAUDE.md rule 9);
- the arguments are a pydantic model, frozen like every model nested in it, so a
  validated call cannot change before it runs; its JSON schema (`model_json_schema()`,
  asked for once, checked, and stored as the one the model is shown) is walked recursively, `$defs` included. Every object says
  `additionalProperties: false`, which also refuses `dict[str, Any]`, and every value
  has a type, which refuses `Any` and a bare `list`. A JSON-in-a-string value
  (`Json[...]`) is refused: the object inside it would escape both checks;
- no name an argument can be sent under matches the identity pattern: the field names,
  aliases, alias paths and alias choices of the model and of every model nested in it
  (in lists, tuples, unions and Annotated), and the schema's property names. The match
  is a substring, so it also refuses names such as username, ownership or parole: a
  field gets another name. Names are plain ASCII, so a homoglyph cannot slip past the
  pattern. The advisor comes from the verified login only (CLAUDE.md rule 8).

validate_call() turns the model's arguments into a ValidCall, or a RejectedCall with
structured errors: a type, a location and a message. No input value is copied into an
error: the message is pydantic's only for the error types in SAFE_MESSAGES, whose
templates name the rule and never the input; any other type (a custom validator, an
unknown union tag) gets FIXED_MESSAGE. The location is pydantic's and can carry key
names the model sent, so traces record error types only (M1). Keys that match the
identity pattern get an `identity_argument` error of their own; any other unknown key
is refused as `extra_forbidden`, since every model forbids extra keys.

- the argument models are a closed world (M1 PR 13; plan default 11, an allowlist rather
  than a list of refused features). A field's type is one of str, int, bool,
  datetime.date, a Literal of str, int or bool, a tuple of reviewed types (tuple[X, ...]
  or fixed), X | None, or a nested frozen model held to the same rules. Its metadata
  holds Field constraints only (min and max length, ge, gt, le, lt, multiple_of,
  pattern, strict). Its Field sets nothing but a default of a reviewed type that its
  own field accepts (checked strictly: pydantic does not validate defaults), a
  description, title, examples and plain-name aliases (no AliasPath or AliasChoices,
  which validate other keys than the schema shows). The model's config sets extra and
  frozen only. Its metaclass is pydantic's, every base is a pydantic model (a mixin's
  method would win over BaseModel's), and every own namespace holds only what Python
  and pydantic write there (CLASS_NAMESPACE): no method, descriptor, property, class
  attribute, validator, serializer or computed field. So a model cannot rewrite its
  input (a before, after, wrap or plain validator, on a field or the model, or a
  method such as model_validate, however it is bound) or show another schema than it
  validates (WithJsonSchema, SkipJsonSchema, json_schema_extra, a RootModel, a schema
  hook).

validate_call() caps the raw arguments before pydantic sees them (M1 PR 13; PR 6
sec-11, PR 8 sec-9), in this order: nested deeper than MAX_DEPTH containers
(`too_deep`); not JSON, such as a set, bytes or NaN (`not_json`); larger than
MAX_ARGUMENT_BYTES as compact UTF-8 JSON (`too_large`); a key or a text holding a
control character (`control_character`: C0, DEL and C1, except tab, LF and CR, which an
e-mail body needs). Identity keys are still named beside a cap. Until M2 these pass:
CR and LF in recipients, a subject or a title; bidi, zero-width and tag characters;
U+2028 and U+2029; private-use characters (the plan, "Left out of M1").

What this does not see: a handler that ignores its validated arguments; a field with a
harmless name and a harmful meaning. Row-level security is the boundary; the registry
keeps identity out of the model's reach.
"""

from __future__ import annotations

import datetime
import json
import re
import types
import unicodedata
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    RootModel,
    Strict,
    TypeAdapter,
    ValidationError,
)
from pydantic.fields import FieldInfo

from app.harness.context import Untrusted

# Words that name whom a call acts for. Matched case-insensitively against the name with
# every separator removed, so advisor_id, advisorId, AdvisorID, advisor-id and advisorid
# all match: stricter than a camelCase split into words, which advisorid would pass.
IDENTITY_TERMS = (
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
    "mitarbeiter",  # plan amendment (Timur, 2026-11-03)
)
_FLAT_TERMS = tuple(term.replace("_", "") for term in IDENTITY_TERMS)
# The Messages API's rule for tool names.
TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
# Argument names: plain ASCII, so the identity pattern sees what the model sees.
ARGUMENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# JSON-schema keywords whose values are schemas, by shape.
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = (
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
)
# A schema with one of these says what it accepts; one without accepts anything.
_TYPED = ("type", "$ref", "enum", "const", "anyOf", "oneOf", "allOf")
# pydantic's Json[...]: a string whose content is another schema.
_JSON_IN_STRING = ("contentSchema", "contentMediaType")
# Error types whose pydantic message names the rule and never the input (pydantic 2.14).
SAFE_MESSAGES = frozenset(
    {
        "bool_parsing",
        "bool_type",
        "date_from_datetime_inexact",
        "date_from_datetime_parsing",
        "date_parsing",
        "date_type",
        "dict_type",
        "enum",
        "extra_forbidden",
        "float_parsing",
        "float_type",
        "frozen_instance",
        "greater_than",
        "greater_than_equal",
        "int_from_float",
        "int_parsing",
        "int_type",
        "invalid_key",
        "less_than",
        "less_than_equal",
        "list_type",
        "literal_error",
        "missing",
        "model_attributes_type",
        "model_type",
        "string_pattern_mismatch",
        "string_too_long",
        "string_too_short",
        "string_type",
        "too_long",
        "too_short",
        "tuple_type",
        "union_tag_not_found",
    }
)
FIXED_MESSAGE = "the value does not fit the schema"

# The caps on raw arguments (M1 PR 13), and their fixed messages.
MAX_DEPTH = 4  # containers on the deepest path; the arguments object is the first
MAX_ARGUMENT_BYTES = 32 * 1024  # compact JSON, UTF-8
TOO_DEEP = f"the arguments nest deeper than {MAX_DEPTH} levels"
TOO_LARGE = f"the arguments are larger than {MAX_ARGUMENT_BYTES // 1024} KiB as JSON"
CONTROL_CHARACTER = "a key or a text holds a control character"
NOT_JSON = "the arguments are not JSON"
TEXT_CONTROLS = frozenset("\t\n\r")  # the control characters a text may hold

# The closed world of argument models (M1 PR 13).
REVIEWED_SCALARS = (str, int, bool, datetime.date)
REVIEWED_LITERALS = (str, int, bool)
# pydantic keeps Field's bounds as annotated_types objects (a dependency of pydantic, not
# of ours, so named here rather than imported), and strict as pydantic's Strict.
REVIEWED_BOUNDS = frozenset({"MinLen", "MaxLen", "Ge", "Gt", "Le", "Lt", "MultipleOf"})
# pydantic keeps Field(pattern=...) in a general metadata object; only this key.
REVIEWED_GENERAL_METADATA = frozenset({"pattern"})
# What a Field may set (FieldInfo._attributes_set; constraints go to its metadata).
REVIEWED_FIELD_ATTRIBUTES = frozenset(
    {
        "annotation",
        "default",
        "description",
        "title",
        "examples",
        "alias",
        "validation_alias",
        "serialization_alias",
        "alias_priority",
    }
)
REVIEWED_CONFIG = frozenset({"extra", "frozen"})
# What a model's own class may hold in its namespace: what Python and pydantic 2.14 write
# there (measured on the MVP models), model_config and the docstring. Fields are not in
# it: pydantic takes their defaults out. Anything else, a method, a descriptor, a class
# attribute, is refused, so a model cannot rewrite its input or show another schema.
CLASS_NAMESPACE = frozenset(
    {
        "__module__",
        "__qualname__",
        "__doc__",
        "__annotations__",
        "__firstlineno__",
        "__static_attributes__",
        "__abstractmethods__",
        "_abc_impl",
        "__weakref__",
        "__dict__",
        "__class_vars__",
        "__private_attributes__",
        "__signature__",
        "__pydantic_complete__",
        "__pydantic_computed_fields__",
        "__pydantic_core_schema__",
        "__pydantic_custom_init__",
        "__pydantic_decorators__",
        "__pydantic_extra_info__",
        "__pydantic_fields__",
        "__pydantic_generic_metadata__",
        "__pydantic_parent_namespace__",
        "__pydantic_post_init__",
        "__pydantic_serializer__",
        "__pydantic_setattr_handlers__",
        "__pydantic_validator__",
        "model_config",
    }
)
_DECORATOR_KINDS = (
    "validators",
    "field_validators",
    "root_validators",
    "model_validators",
    "field_serializers",
    "model_serializers",
    "computed_fields",
)


def identity_term(name: str) -> str | None:
    """The identity term in name, after NFKC and case folding; None if there is none."""
    flat = re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKC", name).casefold())
    return next(
        (term for term, f in zip(IDENTITY_TERMS, _FLAT_TERMS, strict=True) if f in flat), None
    )


class Effect(StrEnum):
    READ = "read"
    DRAFT = "draft"
    SIDE_EFFECT = "side_effect"


class DataClass(StrEnum):
    """The most sensitive data a tool reads or writes."""

    PUBLIC = "public"  # e.g. product sheets
    INTERNAL = "internal"  # the bank's own, about no client
    CONFIDENTIAL = "confidential"  # client data, under bank secrecy


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    result: Any  # a type, or a parameterised generic such as Untrusted[list[X]]
    effect: Effect
    data_class: DataClass
    requires_approval: bool


class RegistrationError(ValueError):
    """A spec the registry refuses; problems names every rule it breaks."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class CallError:
    type: str
    loc: tuple[str | int, ...]
    msg: str


@dataclass(frozen=True)
class ValidCall:
    spec: ToolSpec
    arguments: BaseModel


@dataclass(frozen=True)
class RejectedCall:
    tool: str | None  # None when the model named no registered tool
    errors: tuple[CallError, ...]


def _name_problems(name: object, where: str) -> list[str]:
    if type(name) is not str:
        return [f"{where}: argument name {name!r} is not a str"]
    problems = []
    if not ARGUMENT_NAME.fullmatch(name):
        problems.append(
            f"{where}: argument name {name!r} is not plain ASCII ({ARGUMENT_NAME.pattern})"
        )
    term = identity_term(name)
    if term is not None:
        problems.append(f"{where}: argument name {name!r} names an identity ({term})")
    return problems


def schema_problems(schema: object, root: str = "schema") -> list[str]:
    """Rule breaks in a JSON schema: open objects, untyped values, identity names."""
    problems: list[str] = []
    stack: list[tuple[str, object]] = [(root, schema)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict) or not any(key in node for key in _TYPED):
            problems.append(f"{path}: no type (Any or a bare container is refused)")
            continue
        types = node.get("type")
        types = types if isinstance(types, list) else [types]
        if any(key in node for key in _JSON_IN_STRING):
            problems.append(f"{path}: a JSON-in-a-string value (Json[...]) is refused")
        is_object = "object" in types or "properties" in node
        if is_object and node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties is not false (extra='forbid')")
        if "array" in types and "items" not in node and "prefixItems" not in node:
            problems.append(f"{path}.items: no type (Any or a bare container is refused)")
        for name in node.get("properties", {}):
            problems += _name_problems(name, f"{path}.properties")
        for key in _SCHEMA_MAPS:
            stack += [(f"{path}.{key}.{name}", sub) for name, sub in node.get(key, {}).items()]
        for key in _SCHEMA_LISTS:
            stack += [(f"{path}.{key}[{n}]", sub) for n, sub in enumerate(node.get(key, []))]
        for key in _SCHEMA_ONE:
            if key in node and node[key] is not False:
                stack.append((f"{path}.{key}", node[key]))
    return sorted(set(problems))


def _alias_names(alias: object) -> list[object]:
    if alias is None:
        return []
    if isinstance(alias, AliasChoices):
        return [name for choice in alias.choices for name in _alias_names(choice)]
    if isinstance(alias, AliasPath):
        return [part for part in alias.path if isinstance(part, str)]
    return [alias]


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    found, stack = [], [annotation]
    while stack:
        item = stack.pop()
        if isinstance(item, type) and issubclass(item, BaseModel):
            found.append(item)
        else:
            stack += get_args(item)
    return found


def _is_bound(item: object) -> bool:
    kind = type(item)
    return kind.__module__ == "annotated_types" and kind.__qualname__ in REVIEWED_BOUNDS


def _is_general_metadata(item: object) -> bool:
    kind = type(item)
    return kind.__module__ == "pydantic._internal._fields" and kind.__qualname__.endswith(
        "_PydanticGeneralMetadata"
    )


def _field_info_problems(info: FieldInfo, where: str) -> list[str]:
    """What a Field sets beyond the reviewed attributes, and its metadata."""
    chosen = getattr(info, "_attributes_set", None)
    if not isinstance(chosen, dict):
        return [f"{where}: a Field this pydantic cannot show for review"]
    problems = [
        f"{where}: Field({name}=...) is not reviewed"
        for name in sorted(set(chosen) - REVIEWED_FIELD_ATTRIBUTES)
    ]
    if info.json_schema_extra is not None and "json_schema_extra" not in chosen:
        problems.append(f"{where}: Field(json_schema_extra=...) is not reviewed")
    if "default" in chosen and not _reviewed_default(info.default):
        problems.append(f"{where}: a default of type {type(info.default).__name__} is not reviewed")
    aliases = (info.alias, info.validation_alias, info.serialization_alias)
    if any(alias is not None and type(alias) is not str for alias in aliases):
        # An AliasPath or AliasChoices validates other keys than the schema shows.
        problems.append(f"{where}: an alias other than a plain name is refused")
    return problems + _metadata_problems(info.metadata, where)


def _default_problems(field: FieldInfo, where: str) -> list[str]:
    """A default that its own field would refuse: pydantic does not validate defaults, so
    it would reach the handler unchecked. The value is not named."""
    if "default" not in (getattr(field, "_attributes_set", None) or {}):
        return []
    if not _reviewed_default(field.default):
        return []  # reported by _field_info_problems
    annotation = field.annotation
    if field.metadata:
        annotation = Annotated[(annotation, *field.metadata)]  # type: ignore[valid-type]
    try:
        TypeAdapter(annotation).validate_python(field.default, strict=True)
    except Exception:  # noqa: BLE001 - any failure to fit is the same refusal
        return [f"{where}: the default does not fit its own field"]
    return []


def _metadata_problems(metadata: list[Any] | tuple[Any, ...], where: str) -> list[str]:
    problems = []
    for item in metadata:
        if isinstance(item, FieldInfo):
            problems += _field_info_problems(item, where)
        elif (
            _is_bound(item)
            or type(item) is Strict
            or (_is_general_metadata(item) and set(vars(item)) <= REVIEWED_GENERAL_METADATA)
        ):
            continue
        else:
            problems.append(f"{where}: {type(item).__name__} is not a reviewed constraint")
    return problems


def _reviewed_default(value: object) -> bool:
    if type(value) is tuple:
        return all(_reviewed_default(item) for item in value)
    return value is None or type(value) in REVIEWED_SCALARS


def _type_problems(annotation: object, where: str) -> list[str]:
    """Where annotation leaves the reviewed types (nested models are checked as models)."""
    problems: list[str] = []
    stack = [annotation]
    while stack:
        item = stack.pop()
        origin, args = get_origin(item), get_args(item)
        if any(item is scalar for scalar in REVIEWED_SCALARS):
            continue
        if origin is Annotated:
            stack.append(args[0])
            problems += _metadata_problems(args[1:], where)
        elif origin is Literal and all(type(value) in REVIEWED_LITERALS for value in args):
            continue
        elif origin in (Union, types.UnionType) and len(args) == 2 and type(None) in args:
            stack += [arg for arg in args if arg is not type(None)]
        elif origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            stack.append(args[0])
        elif origin is tuple and args and Ellipsis not in args:
            stack += list(args)
        elif isinstance(item, type) and issubclass(item, BaseModel) and origin is None:
            continue  # a nested model: model_problems() checks it as a model
        else:
            problems.append(f"{where}: {item!r} is not a reviewed type")
    return problems


def _class_problems(model: type[BaseModel]) -> list[str]:
    """The model's classes: pydantic's metaclass, no base that is not a pydantic model
    (a mixin's method would win over BaseModel's), and nothing in any own namespace
    beyond CLASS_NAMESPACE (pydantic's own __hash__ for a frozen model aside)."""
    name = model.__name__
    problems = []
    if type(model) is not type(BaseModel):
        problems.append(f"{name}: a metaclass other than pydantic's is refused")
    for cls in model.__mro__:
        if cls in (BaseModel, RootModel, object):
            continue
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            problems.append(f"{name}: inherits from {cls.__qualname__}, not a pydantic model")
            continue
        for attribute, value in vars(cls).items():
            if attribute in CLASS_NAMESPACE:
                continue
            made_by = getattr(value, "__module__", "") or ""
            if attribute == "__hash__" and made_by.startswith("pydantic."):
                continue  # the hash pydantic writes for a frozen model
            problems.append(f"{name}: defines {attribute}, only fields may")
    return problems


def _closed_world_problems(model: type[BaseModel]) -> list[str]:
    """Where one model leaves the closed world (M1 PR 13)."""
    name = model.__name__
    problems = []
    if issubclass(model, RootModel):
        problems.append(f"{name}: a RootModel is refused (it shows its root's schema)")
    decorators = model.__pydantic_decorators__
    kinds = [kind for kind in _DECORATOR_KINDS if getattr(decorators, kind)]
    if kinds:
        problems.append(
            f"{name}: a custom validator, serializer or computed field is refused ({', '.join(kinds)})"
        )
    unreviewed = sorted(set(model.model_config) - REVIEWED_CONFIG)
    if unreviewed:
        problems.append(f"{name}: model_config {unreviewed} is not reviewed (extra, frozen only)")
    problems += _class_problems(model)
    for field_name, field in model.model_fields.items():
        where = f"{name}.{field_name}"
        problems += _field_info_problems(field, where)
        problems += _type_problems(field.annotation, where)
        problems += _default_problems(field, where)
    return problems


def model_problems(model: type[BaseModel]) -> list[str]:
    """Identity or non-ASCII names that model, or a model nested in it, accepts; any of
    those models that is not frozen; and where any of them leaves the closed world."""
    problems: list[str] = []
    seen: set[type[BaseModel]] = set()
    stack = [model]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current.model_config.get("frozen") is not True:
            problems.append(f"{current.__name__}: not frozen (frozen=True)")
        problems += _closed_world_problems(current)
        for field_name, field in current.model_fields.items():
            names = [field_name, *_alias_names(field.alias), *_alias_names(field.validation_alias)]
            for name in dict.fromkeys(names):
                problems += _name_problems(name, f"{current.__name__}.{field_name}")
            stack += _nested_models(field.annotation)
    return sorted(set(problems))


def _identity_keys(arguments: object) -> list[tuple[str | int, ...]]:
    """Where in arguments a key matches the identity pattern (iterative: depth is free)."""
    found = []
    stack: list[tuple[tuple[str | int, ...], object]] = [((), arguments)]
    while stack:
        loc, node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and identity_term(key) is not None:
                    found.append((*loc, key))
                stack.append(((*loc, key), value))
        elif isinstance(node, list | tuple):
            stack += [((*loc, index), value) for index, value in enumerate(node)]
    return sorted(found, key=repr)


def _cap_errors(arguments: object) -> list[CallError]:
    """The caps on raw arguments, in order: depth, then JSON and size, then control
    characters. Iterative and linear (a queue), and it stops below MAX_DEPTH, so any
    depth and any width is safe to inspect."""
    queue: deque[tuple[tuple[str | int, ...], object, int]] = deque([((), arguments, 1)])
    texts: list[tuple[tuple[str | int, ...], str]] = []
    while queue:
        loc, node, depth = queue.popleft()
        if isinstance(node, dict | list | tuple):
            if depth > MAX_DEPTH:
                return [CallError("too_deep", loc, TOO_DEEP)]
            items = node.items() if isinstance(node, dict) else enumerate(node)
            for key, value in items:
                if isinstance(node, dict) and isinstance(key, str):
                    texts.append(((*loc, key), key))
                queue.append(((*loc, key), value, depth + 1))
        elif isinstance(node, str):
            texts.append((loc, node))
    try:
        sent = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        # A set, bytes, an object or NaN: the loop sends JSON only, so this fails closed
        # for any other caller (pydantic would turn bytes into a str unchecked).
        return [CallError("not_json", (), NOT_JSON)]
    if len(sent.encode("utf-8", "surrogatepass")) > MAX_ARGUMENT_BYTES:
        return [CallError("too_large", (), TOO_LARGE)]
    found = {loc for loc, text in texts if _has_control(text)}
    return [
        CallError("control_character", loc, CONTROL_CHARACTER) for loc in sorted(found, key=repr)
    ]


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" and char not in TEXT_CONTROLS for char in text)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._schemas: dict[str, str] = {}

    def problems(self, spec: ToolSpec) -> list[str]:
        """Every rule spec breaks; empty when register() would admit it."""
        return self._check(spec)[0]

    def _check(self, spec: ToolSpec) -> tuple[list[str], str | None]:
        """The rules spec breaks, and the arguments schema they were checked on, as JSON.

        The schema is asked for once: what register() stores, and M1 sends, is the text
        checked here, never a second answer from the model class (Fable's review).
        """
        problems: list[str] = []
        if type(spec.name) is not str or not TOOL_NAME.fullmatch(spec.name):
            problems.append(f"tool name {spec.name!r} does not match {TOOL_NAME.pattern}")
        elif spec.name in self._specs:
            problems.append(f"{spec.name}: already registered")
        if type(spec.description) is not str or not spec.description.strip():
            problems.append("description: empty")
        if type(spec.effect) is not Effect:
            problems.append(f"effect {spec.effect!r} is not an Effect")
        if type(spec.data_class) is not DataClass:
            problems.append(f"data class {spec.data_class!r} is not a DataClass")
        if type(spec.requires_approval) is not bool:
            problems.append(f"requires_approval {spec.requires_approval!r} is not a bool")
        elif spec.effect in (Effect.DRAFT, Effect.SIDE_EFFECT) and not spec.requires_approval:
            problems.append(f"a {spec.effect} tool needs requires_approval=True")
        if spec.effect is Effect.READ and not (
            get_origin(spec.result) is Untrusted and get_args(spec.result)
        ):
            problems.append(f"a read tool returns Untrusted[...], not {spec.result!r}")
        arguments = spec.arguments
        if not (isinstance(arguments, type) and issubclass(arguments, BaseModel)):
            problems.append(f"arguments {arguments!r} is not a pydantic BaseModel class")
            return problems, None
        try:
            checked = json.dumps(arguments.model_json_schema())
        except Exception as error:  # noqa: BLE001 - a model without a JSON schema is refused
            return [*problems, f"arguments: no JSON schema ({type(error).__name__})"], None
        problems += schema_problems(json.loads(checked)) + model_problems(arguments)
        return problems, checked

    def register(self, spec: ToolSpec) -> None:
        problems, checked = self._check(spec)
        if problems or checked is None:
            raise RegistrationError(problems)
        self._specs[spec.name] = spec
        self._schemas[spec.name] = checked

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def schema(self, name: str) -> dict[str, Any]:
        """The arguments schema as checked at registration, a fresh copy: what M1 sends."""
        return json.loads(self._schemas[name])

    def validate_call(self, name: object, arguments: object) -> ValidCall | RejectedCall:
        spec = self._specs.get(name) if type(name) is str else None
        if spec is None:
            return RejectedCall(None, (CallError("unknown_tool", (), "no tool by that name"),))
        errors = [
            CallError("identity_argument", loc, "arguments carry no identity; the login does")
            for loc in _identity_keys(arguments)
        ]
        capped = _cap_errors(arguments)
        if capped:
            # Before pydantic sees them (M1 PR 13).
            return RejectedCall(spec.name, tuple(errors + capped))
        try:
            parsed = spec.arguments.model_validate(arguments)
        except ValidationError as invalid:
            errors += [
                CallError(
                    error["type"],
                    tuple(error["loc"]),
                    error["msg"] if error["type"] in SAFE_MESSAGES else FIXED_MESSAGE,
                )
                for error in invalid.errors(
                    include_url=False, include_input=False, include_context=False
                )
            ]
        if errors:
            return RejectedCall(spec.name, tuple(errors))
        return ValidCall(spec, parsed)
