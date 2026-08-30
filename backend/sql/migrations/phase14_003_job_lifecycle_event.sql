-- Phase 14 S4-6 lifecycle lookup index.  Additive and safe for mixed fleets.
ALTER TABLE `job`
  ADD KEY `idx_job_lifecycle_version` (`version`,`delist_reason`,`deleted_at`);

-- Lifecycle events are persisted by phase14_001 domain_outbox_event.  This
-- migration intentionally adds no trigger, preserving service-owned writes.
