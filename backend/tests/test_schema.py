from app.db import apply_schema, execute, fetch_all, fetch_one, migrate

TABLES = {"orgs", "teams", "people", "activity_events", "tasks", "similarities",
          "team_overlaps", "connections", "connection_events",
          "skills", "skill_labels", "expertise_events", "person_skill_state", "skill_edges", "match_feedback"}
NEW_COLUMNS = {("activity_events", "expertise_extracted"), ("tasks", "required_skills"),
               ("similarities", "temporal_score"), ("similarities", "shared_skills"),
               ("connections", "feedback_applied")}


async def test_tables_created_in_test_schema():
    rows = await fetch_all("SELECT table_name FROM information_schema.tables WHERE table_schema = 'musketeer_test'")
    assert TABLES <= {r["table_name"] for r in rows}


async def test_hypertables_and_cagg():
    rows = await fetch_all("SELECT hypertable_name FROM timescaledb_information.hypertables "
                           "WHERE hypertable_schema = 'musketeer_test'")
    assert {r["hypertable_name"] for r in rows} == {"activity_events", "connection_events",
                                                    "expertise_events", "match_feedback"}
    cagg = await fetch_one("SELECT view_name FROM timescaledb_information.continuous_aggregates "
                           "WHERE view_schema = 'musketeer_test'")
    assert cagg["view_name"] == "connection_daily"


async def test_reset_is_repeatable():
    await apply_schema(reset=True)
    rows = await fetch_all("SELECT count(*) AS n FROM people")
    assert rows[0]["n"] == 0


async def test_migrate_is_idempotent_and_keeps_data():
    await apply_schema(reset=True)
    await execute("INSERT INTO orgs (id, name) VALUES ('org_x', 'X')")
    await migrate()
    await migrate()
    assert (await fetch_one("SELECT count(*) AS n FROM orgs"))["n"] == 1
    rows = await fetch_all("SELECT table_name, column_name FROM information_schema.columns "
                           "WHERE table_schema = 'musketeer_test'")
    assert NEW_COLUMNS <= {(r["table_name"], r["column_name"]) for r in rows}
    await apply_schema(reset=True)
