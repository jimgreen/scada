"""Shared listener settings for the three Web services and local C command endpoints."""

# A single polling interval can make many channels/dashboards connect together.
# This limits pending TCP accepts, not the writer's queue or handler timeouts.
LISTEN_BACKLOG = 128
