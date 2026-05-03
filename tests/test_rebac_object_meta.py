"""Tests for RebacObjectMeta — registration metaclass for non-model classes."""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac.mixins import RebacObjectMeta
from rebac.resources import _resolve_dotted, to_object_ref
from rebac.types import ObjectRef

# ---------------------------------------------------------------------------
# RebacObjectMeta — class creation
# ---------------------------------------------------------------------------


class TestRebacObjectMetaCapture:
    def test_resource_type_stored_on_class(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"

        assert MyView._rebac_resource_type == "angee/view"

    def test_id_attr_stored_on_class(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "operation"

        assert MyView._rebac_id_attr == "operation"

    def test_default_action_stored_on_class(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_default_action = "read"

        assert MyView._rebac_default_action == "read"

    def test_meta_keys_removed_from_meta(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"

        assert not hasattr(MyView.Meta, "rebac_resource_type")

    def test_no_meta_no_error(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            pass

        # Use vars() to catch a future regression where a default
        # `_rebac_resource_type = None` is added to the metaclass —
        # `hasattr` would return True for that and silently pass.
        assert "_rebac_resource_type" not in vars(MyView)

    def test_does_not_use_django_meta(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"

        assert not hasattr(MyView, "_meta")

    def test_subclass_inherits_resource_type(self) -> None:
        class Base(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"

        class Child(Base):
            pass

        assert Child._rebac_resource_type == "angee/view"

    def test_shared_meta_does_not_mutate_parent(self) -> None:
        """A subclass that re-uses a parent's `Meta` must not strip its keys.

        Regression: `_capture_rebac_meta` previously called `delattr(meta, ...)`
        unconditionally, which wiped the captured keys off the shared `Meta`
        on the *parent* — so any later subclass instantiation lost them.
        """

        class Base(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "operation"

        class Child(Base):
            Meta = Base.Meta  # explicit shared reference

        assert Child._rebac_resource_type == "angee/view"
        assert Child._rebac_id_attr == "operation"
        # Re-instantiate a third class against the same Meta — would fail if
        # the second instantiation had stripped the parent's attributes.

        class GrandChild(Base):
            Meta = Base.Meta

        assert GrandChild._rebac_resource_type == "angee/view"


# ---------------------------------------------------------------------------
# _resolve_dotted
# ---------------------------------------------------------------------------


class TestResolveDotted:
    def test_flat(self) -> None:
        class Obj:
            operation = "demo.notes.list"

        assert _resolve_dotted(Obj(), "operation") == "demo.notes.list"

    def test_two_levels(self) -> None:
        class Source:
            operation = "demo.notes.list"

        class ViewMeta:
            source = Source()

        class Obj:
            _angee_view_meta = ViewMeta()

        assert _resolve_dotted(Obj(), "_angee_view_meta.source.operation") == "demo.notes.list"

    def test_missing_attr_raises(self) -> None:
        class Obj:
            pass

        with pytest.raises(AttributeError):
            _resolve_dotted(Obj(), "nonexistent")


# ---------------------------------------------------------------------------
# to_object_ref with RebacObjectMeta-tagged classes
# ---------------------------------------------------------------------------


class TestToObjectRefObjectMeta:
    def test_flat_id_attr(self) -> None:
        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "operation"

        obj = MyView()
        obj.operation = "demo.notes.list"
        ref = to_object_ref(obj)
        assert ref == ObjectRef("angee/view", "demo.notes.list")

    def test_dotted_id_attr(self) -> None:
        class Source:
            operation = "demo.notes.list"

        class ViewMeta:
            source = Source()

        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "_angee_view_meta.source.operation"

        obj = MyView()
        obj._angee_view_meta = ViewMeta()
        ref = to_object_ref(obj)
        assert ref == ObjectRef("angee/view", "demo.notes.list")

    def test_no_resource_type_falls_through(self) -> None:
        class Plain:
            pass

        with pytest.raises(TypeError, match="Cannot resolve"):
            to_object_ref(Plain())

    def test_unresolvable_id_attr_raises_typeerror(self) -> None:
        """Regression for the AttributeError → TypeError contract."""

        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "missing"

        with pytest.raises(TypeError, match="rebac_id_attr='missing'"):
            to_object_ref(MyView())

    def test_prefix_applied_to_object_meta_path(self) -> None:
        """`REBAC_TYPE_PREFIX` must apply to the RebacObjectMeta branch
        the same way it applies to Django models — otherwise multi-package
        deployments emit cross-tenant collisions.
        """

        class MyView(metaclass=RebacObjectMeta):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "operation"

        obj = MyView()
        obj.operation = "demo.notes.list"
        with override_settings(REBAC_TYPE_PREFIX="tenantA/"):
            ref = to_object_ref(obj)
        assert ref == ObjectRef("tenantA/angee/view", "demo.notes.list")
