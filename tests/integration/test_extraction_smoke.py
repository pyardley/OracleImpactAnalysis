"""Opt-in integration test against a real Oracle instance - strictly read-only
(SELECT against data-dictionary views only, never DDL/DML). Skipped by default;
run with `pytest -m integration` against an instance configured via .env.
"""

import pytest

from oia.config import get_settings
from oia.extraction import extract_metadata
from oia.graph.pipeline import build_full_graph

pytestmark = pytest.mark.integration


@pytest.fixture
def settings():
    return get_settings()


def test_extract_runs_cleanly_and_produces_a_graph(settings, store):
    stats = extract_metadata(settings, store)
    assert stats.objects_processed > 0
    assert stats.objects_failed == 0

    graph_stats = build_full_graph(settings, store)
    assert graph_stats.node_count >= stats.objects_processed
