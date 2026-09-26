"""SQL for the derived states in spec §5, shared by the read endpoints. Never stored."""

# Latest connection per unordered pair (a < b).
LATEST_CONN = """
latest AS (
  SELECT DISTINCT ON (LEAST(requester_id, helper_id), GREATEST(requester_id, helper_id))
         LEAST(requester_id, helper_id) AS a, GREATEST(requester_id, helper_id) AS b,
         id, status, message_count, match_score
  FROM connections
  ORDER BY LEAST(requester_id, helper_id), GREATEST(requester_id, helper_id), created_at DESC
)"""

# Edge state from the latest connection's status (NULL -> potential).
EDGE_STATE = """CASE WHEN {s} IN ('suggested', 'requested') THEN 'pending'
                     WHEN {s} IN ('accepted', 'active') THEN 'connected'
                     ELSE 'potential' END"""

# Person has an open/notified task older than AMBER_AFTER_MIN with no accepted/active connection for it.
NEEDS_CONNECTION = """EXISTS (
  SELECT 1 FROM tasks t
  WHERE t.person_id = {p} AND t.status IN ('open', 'notified')
    AND t.created_at < now() - make_interval(mins => %(amber_min)s)
    AND NOT EXISTS (SELECT 1 FROM connections c WHERE c.task_id = t.id AND c.status IN ('accepted', 'active')))"""


def edge_state(status_col: str) -> str:
    return EDGE_STATE.format(s=status_col)


def needs_connection(person_col: str) -> str:
    return NEEDS_CONNECTION.format(p=person_col)
