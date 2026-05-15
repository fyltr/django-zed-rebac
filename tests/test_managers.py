from __future__ import annotations

from rebac.managers import RebacManager, RebacQuerySet
from tests.testapp.models import Post


class CustomPostQuerySet(RebacQuerySet):
    def titled(self, title: str) -> CustomPostQuerySet:
        return self.filter(title=title)


def test_rebac_manager_from_queryset_preserves_queryset_class() -> None:
    manager = RebacManager.from_queryset(CustomPostQuerySet)()
    manager.model = Post

    queryset = manager.get_queryset()

    assert isinstance(queryset, CustomPostQuerySet)
    assert queryset.model is Post


def test_rebac_manager_from_queryset_copied_methods_use_custom_queryset() -> None:
    manager = RebacManager.from_queryset(CustomPostQuerySet)()
    manager.model = Post

    queryset = manager.titled("Notes")  # type: ignore[attr-defined]

    assert isinstance(queryset, CustomPostQuerySet)
    assert str(queryset.query)
