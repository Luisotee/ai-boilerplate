"""
Redis Streams implementation for per-user sequential message processing.

This package provides functions to manage Redis Streams for processing
chat messages sequentially per user while allowing concurrent processing
across different users.
"""

from .manager import (
    CONSUMER_ID,
    GROUP_NAME,
    acknowledge_message,
    add_message_to_stream,
    ensure_consumer_group,
    read_stream_messages,
)

__all__ = [
    "add_message_to_stream",
    "ensure_consumer_group",
    "read_stream_messages",
    "acknowledge_message",
    "GROUP_NAME",
    "CONSUMER_ID",
]
