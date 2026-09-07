-- server_schema_online_usage.sql  (MySQL)
--
-- PROPOSED addition to the license server's schema — the license
-- server itself is a separate repo/service (see updater.py's comment
-- referencing "server/app/main.py and the app_versions table in
-- schema.sql", and licensing.py's calls to settings.license_server_url).
-- This file is NOT wired into anything automatically; it's a spec for
-- whoever maintains that server to add, matching the /v1/usage/report
-- endpoint contract documented in usage.py's module docstring.
--
-- Why server-side at all: a client-only usage counter (a local file)
-- can be reset just by deleting it, which defeats the point of a paid
-- monthly-hours quota (see mithracorp.com/mithravoice.html#pricing —
-- Bronze/Silver/Gold online plans each include a fixed hours/mo
-- allowance). The client (usage.py) only ever reports elapsed seconds
-- and displays whatever the server says is left; the server is the
-- only place that can actually be trusted to enforce the cap, exactly
-- like device-activation seat limits already are (see licensing.py's
-- activate()/deactivate_this_device()).
--
-- IMPORTANT: key_code below is VARCHAR(32) to match license_keys.key_code
-- exactly (confirmed via `DESCRIBE license_keys;` — varchar(32), UNI).
-- If that column's definition ever changes, this one and the FOREIGN
-- KEY both need to change with it, or MySQL will refuse the foreign
-- key with error 1215 / errno 150.

-- One row per (key_code, calendar month). A new row is created
-- automatically -- by the server, not this file -- the first time a
-- usage report for a new period arrives; there's deliberately no
-- separate "reset" job needed, since a missing row for the current
-- period IS the reset (seconds_used starts at 0 implicitly).
CREATE TABLE online_usage_periods (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    key_code            VARCHAR(32) NOT NULL,

    -- First day of the billing month (UTC, e.g. '2026-09-01') and the
    -- first day of the NEXT month (exclusive upper bound). Stored as
    -- actual dates rather than derived from updated_at each time, so
    -- "which period does this report belong to" is a stable lookup
    -- even if the server's understanding of a plan's billing anchor
    -- day changes later.
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,

    seconds_used         INT UNSIGNED NOT NULL DEFAULT 0,

    -- Snapshot of the plan's included hours (converted to seconds) AT
    -- THE START of this period. Deliberately copied here rather than
    -- joined from a live "plans" table on every read, so upgrading or
    -- downgrading a plan mid-month never rewrites history for a
    -- period that's already in progress or finished.
    seconds_included     INT UNSIGNED NOT NULL,

    -- Set the moment seconds_used first crosses seconds_included this
    -- period; stays set (not cleared) even if seconds_used is later
    -- adjusted, so "did they ever go over this period" is answerable
    -- without recomputing from the raw counter.
    exceeded_at          TIMESTAMP NULL DEFAULT NULL,

    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uq_online_usage_key_period (key_code, period_start),
    KEY idx_online_usage_key_code (key_code),
    CONSTRAINT fk_online_usage_key_code
        FOREIGN KEY (key_code) REFERENCES license_keys(key_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Example query the /v1/usage/report handler would run (pseudocode —
-- adapt to whatever this server's actual query layer is):
--
--   period = SELECT * FROM online_usage_periods
--            WHERE key_code = :key_code AND period_start = :this_month_start
--   if period is None:
--       included = lookup_plan_included_hours(key_code) * 3600  -- e.g. Bronze=10*3600, Silver=25*3600, Gold=40*3600; NULL/omit enforcement entirely for pay-as-you-go plans, which bill per hour with no monthly cap
--       period = INSERT new row (period_start=this_month_start, period_end=next_month_start, seconds_used=0, seconds_included=included)
--   period.seconds_used += seconds_delta
--   if period.seconds_used >= period.seconds_included and period.exceeded_at is None:
--       period.exceeded_at = now()
--   UPDATE period, updated_at = now()
--   respond with {seconds_used, seconds_included, seconds_remaining, period_end, exceeded}