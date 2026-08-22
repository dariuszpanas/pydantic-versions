# Pydantic Compatibility Matrix

This matrix is the generated-wire policy for the supported Pydantic range
`>=2.12.3,<3.0`. It classifies settings by whether they can be copied into an
object-shaped document contract without running application behavior or
silently changing validation and serialization semantics.

## Field declarations

| Classification | Settings and behavior |
| --- | --- |
| Preserved | `alias`, `alias_priority`, `validation_alias`, `serialization_alias`, direct defaults, zero-argument default factories, `title`, `description`, `examples`, `deprecated`, `validate_default`, string discriminators, and mapping-valued `json_schema_extra` |
| Preserved | Declarative constraints and metadata represented by supported Pydantic or `annotated-types` metadata, including numeric/string bounds, patterns, length, multiples, decimal constraints, union mode, and fail-fast behavior |
| Omitted | `exclude=True` and effective `exclude_if`; the field is absent from generated projections, including nested automatic projections |
| Omitted | `frozen`, `init`, `init_var`, `kw_only`, and `repr`; these describe application-object construction or representation rather than the wire document |
| Omitted | Field validators, field serializers, and other executable `Annotated` behavior; the authoritative current model remains responsible for runtime behavior |
| Rejected | `field_title_generator`, callable discriminators, callable field `json_schema_extra`, custom annotation schema hooks, and custom executable metadata |
| Rejected | Default factories that consume validated data, because generated models intentionally do not run the authoritative model's validation behavior |

Historical patches operate after this field classification. A removed field is
absent by declaration; an excluded field is absent because it is application
state; and a renamed field retains the historical wire name.

## Model configuration

| Classification | `ConfigDict` settings |
| --- | --- |
| Preserved | `extra`, `strict`, `populate_by_name`, `validate_by_alias`, `validate_by_name`, `serialize_by_alias`, `loc_by_alias`, `use_enum_values`, `alias_generator`, `allow_inf_nan`, `coerce_numbers_to_str`, `regex_engine`, string length/case/whitespace settings, `ser_json_bytes`, `ser_json_inf_nan`, `ser_json_temporal`, `ser_json_timedelta`, `val_json_bytes`, `val_temporal_unit`, `json_schema_mode_override`, `json_schema_serialization_defaults_required`, `title`, and `url_preserve_empty_path` |
| Preserved conditionally | Mapping-valued `json_schema_extra`; structural schema keys and callable mutation are rejected |
| Omitted | `cache_strings`, `defer_build`, `from_attributes`, `frozen`, `hide_input_in_errors`, `ignored_types`, `protected_namespaces`, `revalidate_instances`, `use_attribute_docstrings`, `validate_assignment`, `validate_return`, and `validation_error_cause` |
| Rejected | Effective `arbitrary_types_allowed`, `field_title_generator`, `json_encoders`, `model_title_generator`, `plugin_settings`, `polymorphic_serialization`, and `schema_generator` |

The generated model is a document validator and serializer, not a behavioral
copy of the application model. Omitted lifecycle settings therefore do not
mean that the setting is unsupported on the authoritative model; they mean it
does not belong in the generated wire contract.

Preserved `extra="allow"` applies to source wire validation and inspection.
Untyped extras remain on the returned source model, but the private transition
mapping recursively contains declared fields only. Extras therefore do not
populate excluded or historically removed application fields through Python
names or validation aliases, and unrelated extras are not an implicit opaque
relay across family conversion. Declare an extension field or envelope when
that data must participate in migrations or current-model validation.

## Cross-cutting boundaries

- Unresolved generic bases, behavioral dataclasses, typed extras, and custom
  type-alias or annotation hooks are rejected. Concrete generic
  specializations are supported.
- Nested families must be declared at the paths whose child schema histories
  need independent projection. Nested automatic projections apply the same
  field matrix as root projections.
- String content discriminators are preserved in generated JSON Schema and
  validation. Callable discriminators are rejected.
- Validators and serializers are omitted from generated models; use the family
  validation API when authoritative application behavior is required.

The focused unit and integration tests exercise each supported category and
the principal interactions between aliases, nested projections, exclusions,
generic specializations, validators, and discriminators.
