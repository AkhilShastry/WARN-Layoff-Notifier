"""Tests for value parsing.

These cover the formats actually observed across the 50 state sources -- every
case here came from a real cell in a real state's published data.
"""

from datetime import date

import pytest

from warn.normalize import (
    classify_notice_type, clean_text, normalize_employer, parse_date, parse_int,
    record_hash,
)


class TestParseDate:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("3/14/2024", date(2024, 3, 14)),
            ("03/14/2024", date(2024, 3, 14)),
            ("2024-03-14", date(2024, 3, 14)),
            ("March 14, 2024", date(2024, 3, 14)),
            ("14-Mar-24", date(2024, 3, 14)),
            ("Mar 14 2024", date(2024, 3, 14)),
            ("2024-03-14T00:00:00.000", date(2024, 3, 14)),   # Socrata (TX)
            ("20240314", date(2024, 3, 14)),                  # packed (WI)
            (20240314, date(2024, 3, 14)),
            (date(2024, 3, 14), date(2024, 3, 14)),
        ],
    )
    def test_formats(self, value, expected):
        assert parse_date(value) == expected

    def test_excel_serial(self):
        # 45000 days after 1899-12-30.
        assert parse_date(45000) == date(2023, 3, 15)

    def test_range_takes_first(self):
        # New Jersey: "4/10/26 - 11/26/26"
        assert parse_date("4/10/26 - 11/26/26") == date(2026, 4, 10)

    def test_prose_prefix(self):
        # Pennsylvania: "beginning 11/16/2026"
        assert parse_date("beginning 11/16/2026") == date(2026, 11, 16)

    @pytest.mark.parametrize(
        "value", ["", "   ", None, "TBD", "Various", "N/A", "Unknown", "-", "2024"]
    )
    def test_rejects_non_dates(self, value):
        assert parse_date(value) is None

    def test_rejects_implausible_years(self):
        assert parse_date("3/14/1850") is None
        assert parse_date("3/14/2099") is None


class TestParseInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1,234", 1234),
            ("45 employees", 45),
            ("approx. 50", 50),
            ("50-60", 60),      # ranges report the upper bound
            (120.0, 120),
            ("0", 0),
            (75, 75),
        ],
    )
    def test_values(self, value, expected):
        assert parse_int(value) == expected

    @pytest.mark.parametrize("value", ["TBD", "N/A", "", None, "Unknown", "Closure"])
    def test_rejects_non_numbers(self, value):
        assert parse_int(value) is None

    def test_rejects_absurd_counts(self):
        assert parse_int("9999999") is None


class TestCleanText:
    def test_collapses_whitespace(self):
        assert clean_text("  Acme   Corp\n ") == "Acme Corp"

    def test_normalizes_nbsp(self):
        assert clean_text("Acme Corp") == "Acme Corp"

    @pytest.mark.parametrize("value", ["N/A", "n/a", "TBD", "  ", "none", "nan"])
    def test_placeholders_become_none(self, value):
        assert clean_text(value) is None


class TestNormalizeEmployer:
    def test_collapses_legal_suffixes(self):
        assert normalize_employer("Acme Corp.") == normalize_employer("ACME CORPORATION")

    def test_strips_dba_tail(self):
        assert normalize_employer("Acme Inc. dba Acme Foods") == "acme"

    def test_distinct_companies_stay_distinct(self):
        assert normalize_employer("Acme Corp") != normalize_employer("Acne Corp")


class TestClassifyNoticeType:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Plant Closing", "Closure"),
            ("Layoff", "Layoff"),
            ("Closure and Layoff", "Closure and Layoff"),
            ("CL", "Closure"),       # Wisconsin's codes
            ("LO", "Layoff"),
            ("Reduction in Force", "Layoff"),
        ],
    )
    def test_classification(self, value, expected):
        assert classify_notice_type(value) == expected

    def test_empty(self):
        assert classify_notice_type(None) is None


class TestRecordHash:
    def test_is_stable(self):
        args = ("MD", "Acme Corp", "Baltimore", date(2024, 1, 1), None, 50)
        assert record_hash(*args) == record_hash(*args)

    def test_ignores_employer_formatting(self):
        """A state re-typing "Acme Corp." as "ACME CORPORATION" must not look
        like a brand-new layoff notice to the alerting pipeline."""
        a = record_hash("MD", "Acme Corp.", "Baltimore", date(2024, 1, 1), None, 50)
        b = record_hash("MD", "ACME CORPORATION", "baltimore", date(2024, 1, 1), None, 50)
        assert a == b

    def test_differs_on_real_change(self):
        a = record_hash("MD", "Acme", "Baltimore", date(2024, 1, 1), None, 50)
        b = record_hash("MD", "Acme", "Baltimore", date(2024, 1, 1), None, 75)
        assert a != b
