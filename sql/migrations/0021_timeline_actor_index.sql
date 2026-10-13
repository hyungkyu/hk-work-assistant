-- 0021_timeline_actor_index
--
-- Find an account's activity without reading every event.
--
-- HK, 2026-10-13: http://127.0.0.1:8081/backoffice#/unmapped 이 페이지가
-- 너무 느린데?
--
-- The 미확인 계정 screen asks, for each unowned account, where it was active.
-- timeline_events had indexes by person and by container, and none by the
-- external actor id -- which is exactly the column an account nobody owns is
-- known by. Each account was a full scan of the largest table here, two
-- hundred of them per page load, and the 조직도 tile and 봇 block asked
-- again.
--
-- Built in the migration's transaction, so inserts wait while it builds;
-- the collectors run at night, and the table builds this in seconds.

CREATE INDEX IF NOT EXISTS timeline_events_actor_external_idx
    ON timeline_events (source, actor_external_id);
