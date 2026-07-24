"""Public append-only audit interface."""

from daily_insights_api.modules.audit.service import record_audit_event

__all__ = ["record_audit_event"]
