from oia.config.settings import ReportRules
from oia.graph.report_rules import is_report


def test_prefix_match():
    rules = ReportRules(name_prefixes=["RPT_"])
    assert is_report("RETAILDEMO", "RPT_SALES", rules)
    assert not is_report("RETAILDEMO", "SALES_RPT", rules)


def test_suffix_match():
    rules = ReportRules(name_suffixes=["_REPORT"])
    assert is_report("RETAILDEMO", "MONTHLY_REPORT", rules)
    assert not is_report("RETAILDEMO", "REPORT_MONTHLY", rules)


def test_schema_pattern_match():
    rules = ReportRules(schema_patterns=["^REPORTING$"])
    assert is_report("REPORTING", "ANYTHING", rules)
    assert not is_report("RETAILDEMO", "ANYTHING", rules)


def test_allowlist_match_is_owner_qualified():
    rules = ReportRules(allowlist=["RETAILDEMO.SPECIAL_CASE"])
    assert is_report("RETAILDEMO", "SPECIAL_CASE", rules)
    assert not is_report("OTHER", "SPECIAL_CASE", rules)


def test_no_rules_means_not_a_report():
    rules = ReportRules()
    assert not is_report("RETAILDEMO", "CUSTOMERS", rules)
