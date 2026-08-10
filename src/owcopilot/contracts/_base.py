"""Shared invariants for immutable domain-contract value objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator


class _StrictFrozenModel(BaseModel):
    """Reject coercion and keep every contract instance immutable after validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
        revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def require_exact_mapping(cls, value: Any) -> Any:
        """Reject attribute carriers and unknown keys even when core options are overridden."""

        if type(value) is cls:
            return value
        if not isinstance(value, Mapping):
            raise ValueError("immutable contract input must be a mapping")
        unknown = [key for key in value if key not in cls.model_fields]
        if unknown:
            raise ValueError(f"extra inputs are forbidden for immutable contracts: {unknown!r}")
        return value

    @staticmethod
    def _reject_new_validation_options(options: Mapping[str, Any]) -> None:
        """Fail closed on post-2.6 validation options without forwarding them to older Pydantic."""

        unknown = set(options) - {"extra", "by_alias", "by_name"}
        if unknown:
            raise ValueError(f"unsupported validation options: {sorted(unknown)!r}")
        if options.get("extra") not in {None, "forbid"}:
            raise ValueError("extra inputs cannot be enabled for immutable contracts")
        if options.get("by_alias") is not None or options.get("by_name") is not None:
            raise ValueError("alias validation overrides are forbidden for immutable contracts")

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        **options: Any,
    ) -> Self:
        """Validate Python input without allowing a caller to downgrade contract strictness."""

        if strict is False:
            raise ValueError("strict=False is forbidden for immutable contracts")
        if from_attributes is True:
            raise ValueError("from_attributes=True is forbidden for immutable contracts")
        cls._reject_new_validation_options(options)
        return super().model_validate(
            obj,
            strict=True,
            from_attributes=from_attributes,
            context=context,
        )

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        context: Any | None = None,
        **options: Any,
    ) -> Self:
        """Validate JSON in strict JSON mode while preserving native JSON representations."""

        if strict is False:
            raise ValueError("strict=False is forbidden for immutable contracts")
        cls._reject_new_validation_options(options)
        return super().model_validate_json(
            json_data,
            strict=True,
            context=context,
        )

    @classmethod
    def model_validate_strings(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        context: Any | None = None,
        **options: Any,
    ) -> Self:
        """Reject Pydantic's deliberately coercive string-only validation entrypoint."""

        raise ValueError("model_validate_strings is forbidden for immutable contracts")

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy safely; Pydantic's default update path deliberately skips validation."""

        if update is None:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        return self.__class__.model_validate(payload)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Keep Pydantic's deprecated copy API from bypassing validation."""

        if include is not None or exclude is not None:
            raise ValueError("partial copies are forbidden for immutable contracts")
        return self.model_copy(update=update, deep=deep)


__all__ = ["_StrictFrozenModel"]
