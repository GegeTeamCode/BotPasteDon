"""Order state machine for tracking order lifecycle across all bots."""

from shared.constants import (
    ORDER_DETECTED, ORDER_NOTIFIED, ORDER_THREAD_CREATED,
    ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED,
)

# Valid state transitions
TRANSITIONS = {
    ORDER_DETECTED: [ORDER_NOTIFIED, ORDER_FAILED],
    ORDER_NOTIFIED: [ORDER_THREAD_CREATED, ORDER_DETECTED],  # re-detect if thread not created
    ORDER_THREAD_CREATED: [ORDER_DELIVERING, ORDER_FAILED],
    ORDER_DELIVERING: [ORDER_COMPLETED, ORDER_FAILED],
    ORDER_FAILED: [ORDER_DETECTED],  # retry from scratch
}


def is_valid_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, [])
