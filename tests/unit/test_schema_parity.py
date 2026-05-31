"""Schema-parity guard: the test jobs DB must match the editor's real schema.

The `in_memory_db` fixture (and `seed_jobs` helper) build the jobs table by
calling profile-editor/main.py's `_init_jobs_db()` — the single source of truth.
This test pins the resulting column set so the fixture can never silently drift
away from main.py's real schema again (the old hand-written fixture had).
"""

# Exact column set the editor's `_init_jobs_db()` is supposed to create.
EXPECTED_COLUMNS = {
    "id",
    "title",
    "employer",
    "location",
    "job_url",
    "suitability_score",
    "suitability_reason",
    "date_posted",
    "is_remote",
    "job_type",
    "discovered_at",
    "user_rating",
    "notes",
    "hidden",
    "is_applied",
}


def test_jobs_schema_matches_editor_init(in_memory_db):
    rows = in_memory_db.execute("PRAGMA table_info(jobs)").fetchall()
    actual_columns = {row["name"] for row in rows}
    assert actual_columns == EXPECTED_COLUMNS
