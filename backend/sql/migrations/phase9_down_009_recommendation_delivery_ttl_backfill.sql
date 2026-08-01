-- Data backfills are intentionally not reversed. Restoring NULL would extend
-- encrypted-content retention and could overwrite a shorter runtime deadline.
SELECT 1;
