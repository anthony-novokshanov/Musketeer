from app.db import apply_schema, fetch_all, fetch_one

TABLES = {"orgs", "teams", "people", "activity_events", "tasks", "similarities",
          "team_overlaps", "connections", "connection_events"}


async def test_tables_created_in_test_schema():
    rows = await fetch_all("SELECT table_name FROM information_schema.tables WHERE table_schema = 'musketeer_test'")
    assert TABLES <= {r["table_name"] for r in rows}


async def test_hypertables_and_cagg():
    rows = await fetch_all("SELECT hypertable_name FROM timescaledb_information.hypertables "
                           "WHERE hypertable_schema = 'musketeer_test'")
    assert {r["hypertable_name"] for r in rows} == {"activity_events", "connection_events"}
    cagg = await fetch_one("SELECT view_name FROM timescaledb_information.continuous_aggregates "
                           "WHERE view_schema = 'musketeer_test'")
    assert cagg["view_name"] == "connection_daily"


async def test_reset_is_repeatable():
    await apply_schema(reset=True)
    rows = await fetch_all("SELECT count(*) AS n FROM people")
    assert rows[0]["n"] == 0
