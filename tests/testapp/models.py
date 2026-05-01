"""Synthetic models for testing the mixin."""
from __future__ import annotations

from django.db import models

from rebac import RebacMixin


class Folder(RebacMixin, models.Model):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/folder"


class Post(RebacMixin, models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts"
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/post"
