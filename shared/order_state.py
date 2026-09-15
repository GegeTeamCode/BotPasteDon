"""Order state machine for tracking order lifecycle across all bots."""

from shared.constants import (
    ORDER_DETECTED, ORDER_NOTIFIED, ORDER_THREAD_CREATED,
    ORDER_DELIVERING, ORDER_DELIVERED, ORDER_COMPLETED, ORDER_FAILED,
    ORDER_RETRY_PENDING,
)

# Valid state transitions
TRANSITIONS = {
    ORDER_DETECTED: [ORDER_NOTIFIED, ORDER_FAILED],
    # NOTIFIED = captured + handed to the ERP push (erp_synced tracks the push).
    ORDER_NOTIFIED: [ORDER_DELIVERING, ORDER_DELIVERED, ORDER_FAILED, ORDER_DETECTED],
    # Legacy Discord-thread status: only on rows created before 2026-09-15.
    ORDER_THREAD_CREATED: [ORDER_DELIVERING, ORDER_DELIVERED, ORDER_FAILED],
    # DELIVERED = fast-delivered on marketplace, proof still pending → operator can
    # still send proof ("Đã giao") which completes it, or it fails.
    ORDER_DELIVERING: [ORDER_DELIVERED, ORDER_COMPLETED, ORDER_FAILED, ORDER_RETRY_PENDING],
    ORDER_DELIVERED: [ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED],
    ORDER_RETRY_PENDING: [ORDER_DELIVERING, ORDER_FAILED, ORDER_COMPLETED],
    ORDER_FAILED: [ORDER_DETECTED, ORDER_RETRY_PENDING],
}


def is_valid_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, [])
