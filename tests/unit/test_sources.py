from oia.graph.model import object_node_id
from oia.graph.sources import load_object_sources

OWNER = "RETAILDEMO"


def test_load_object_sources_covers_procs_views_and_triggers(store):
    store.bulk_insert(
        "raw_source",
        ["owner", "object_name", "object_type", "body"],
        [(OWNER, "MY_PROC", "PROCEDURE", "PROCEDURE MY_PROC IS BEGIN NULL; END;")],
    )
    store.bulk_insert(
        "raw_view_text",
        ["owner", "view_name", "text", "is_mview"],
        [(OWNER, "MY_VIEW", "SELECT 1 FROM DUAL", 0)],
    )
    store.bulk_insert(
        "raw_triggers",
        ["owner", "trigger_name", "table_owner", "table_name", "triggering_event", "trigger_type", "body"],
        [(OWNER, "MY_TRIGGER", OWNER, "SOME_TABLE", "INSERT", "AFTER EACH ROW", "BEGIN NULL; END;")],
    )
    store.commit()

    sources = load_object_sources(store)

    assert "NULL" in sources[object_node_id(OWNER, "MY_PROC")]
    assert "SELECT 1 FROM DUAL" in sources[object_node_id(OWNER, "MY_VIEW")]
    assert sources[object_node_id(OWNER, "MY_TRIGGER")] == "BEGIN NULL; END;"


def test_load_object_sources_skips_objects_without_source(store):
    sources = load_object_sources(store)
    assert sources == {}
