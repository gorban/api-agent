"""Exact public path table; no slash normalization or redirect."""

from django.urls import re_path

from . import views


urlpatterns = [
    re_path(r"^$", views.landing),
    re_path(r"^app/agent/?$", views.landing),
    re_path(r"^app/agent/internals/health$", views.health),
    re_path(r"^app/agent/internals/health/startup$", views.health, {"probe": "startup"}),
    re_path(r"^app/agent/internals/health/ready$", views.health, {"probe": "ready"}),
    re_path(r"^app/agent/models$", views.models),
    re_path(r"^app/agent/chat$", views.chat),
    re_path(r"^app/agent/chat/stream$", views.chat_stream),
    re_path(r"^app/agent/chat/cancel$", views.chat_cancel),
    re_path(r"^app/agent/chat/heartbeat$", views.chat_heartbeat),
    re_path(r"^app/agent/result-sets/(?P<result_id>[^/]+)$", views.result_set),
    re_path(r"^app/agent/result-sets$", views.result_sets),
]
