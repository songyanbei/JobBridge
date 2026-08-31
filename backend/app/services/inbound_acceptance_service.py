"""Compatibility import for the durable inbound acceptance service."""

from app.services.inbound_acceptance import (
    AcceptanceResult,
    InboundAcceptanceService,
)

__all__ = ["AcceptanceResult", "InboundAcceptanceService"]
