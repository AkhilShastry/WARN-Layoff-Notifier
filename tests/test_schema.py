"""Tests for column mapping.

Every header list here is copied verbatim from a live state source, which is
what makes this suite a regression net: if a synonym change breaks Texas or
Iowa, these fail before a scrape does.
"""

import pytest

from warn.schema import WarnNotice, map_headers, normalize_header


def mapping_for(headers):
    """Header text -> canonical field, for readable assertions."""
    return {headers[i]: field for i, field in map_headers(headers).items()}


class TestNormalizeHeader:
    def test_collapses_punctuation_and_case(self):
        assert normalize_header("Notice Date") == "noticedate"
        assert normalize_header("NOTICE_DATE") == "noticedate"
        assert normalize_header("notice-date") == "noticedate"

    def test_drops_parentheticals(self):
        assert normalize_header("Employees (est.)") == "employees"

    def test_ignores_pandas_placeholders(self):
        assert normalize_header("Unnamed: 3") == ""


class TestRealStateHeaders:
    def test_texas_socrata(self):
        headers = ["notice_date", "job_site_name", "county_name", "wda_name",
                   "total_layoff_number", "layoff_date", "wfdd_received_date",
                   "city_name"]
        m = mapping_for(headers)
        assert m["job_site_name"] == "employer"
        assert m["notice_date"] == "notice_date"
        assert m["layoff_date"] == "effective_date"
        assert m["total_layoff_number"] == "employees"
        assert m["city_name"] == "city"
        assert m["county_name"] == "county"

    def test_iowa_workbook(self):
        headers = ["Company", "Address Line 1", "City", "County", "St", "ZIP",
                   "Notice Type", "Emp #", "Notice Date", "Layoff Date",
                   "Local Workforce Area", "Industry"]
        m = mapping_for(headers)
        assert m["Company"] == "employer"
        assert m["Emp #"] == "employees"
        assert m["Notice Date"] == "notice_date"
        assert m["Layoff Date"] == "effective_date"
        assert m["Local Workforce Area"] == "region"

    def test_wisconsin_sheet(self):
        headers = ["PK", "FK", "PDF", "Company", "City", "AffectedWorkers",
                   "NoticeRcvd", "NoticeType", "LayoffBeginDate",
                   "NAICSDescription", "County", "WDA"]
        m = mapping_for(headers)
        assert m["Company"] == "employer"
        assert m["AffectedWorkers"] == "employees"
        assert m["NoticeRcvd"] == "notice_date"
        assert m["LayoffBeginDate"] == "effective_date"

    def test_north_carolina_prefixed_header(self):
        headers = ["County", "Warn Number", "Date of Notice", "Date Received by NC",
                   "Effective Date", "WARN Notice: WARN Notice Name",
                   "WARN notice type", "Number affected at this location", "City"]
        m = mapping_for(headers)
        assert m["WARN Notice: WARN Notice Name"] == "employer"
        assert m["Number affected at this location"] == "employees"

    def test_washington_gridview(self):
        headers = ["Company", "Location", "Layoff Start Date", "# of Workers",
                   "Closure Layoff", "Type of Layoff", "Received Date", "Notice"]
        m = mapping_for(headers)
        assert m["Company"] == "employer"
        assert m["# of Workers"] == "employees"
        assert m["Received Date"] == "notice_date"
        assert m["Layoff Start Date"] == "effective_date"


class TestConflictResolution:
    def test_two_date_columns_do_not_collapse(self):
        headers = ["Business Name", "Date Received", "Date of Layoff", "Workers Affected"]
        m = mapping_for(headers)
        assert m["Date Received"] == "notice_date"
        assert m["Date of Layoff"] == "effective_date"

    def test_each_field_claimed_once(self):
        headers = ["Company Name", "City", "Notice Date", "Layoff Date",
                   "Number of Employees", "Type"]
        fields = list(map_headers(headers).values())
        assert len(fields) == len(set(fields)), "a field was assigned twice"

    def test_free_text_column_is_not_a_headcount(self):
        """Colorado publishes "Reason for Layoffs" next to "Total Notified";
        the prose column must not win the employees slot."""
        headers = ["Company", "WARN Date", "Total Notified", "Begin Date",
                   "Reason for Layoffs"]
        m = mapping_for(headers)
        assert m["Total Notified"] == "employees"
        assert m.get("Reason for Layoffs") != "employees"

    def test_unknown_columns_are_left_alone(self):
        headers = ["Company", "Sparkle Index", "Notice Date"]
        m = mapping_for(headers)
        assert "Sparkle Index" not in m

    def test_overrides_win(self):
        headers = ["Title", "Received", "Layoff date(s)"]
        m = {headers[i]: f
             for i, f in map_headers(headers, overrides={"title": "employer"}).items()}
        assert m["Title"] == "employer"


class TestWarnNotice:
    def test_requires_employer(self):
        assert not WarnNotice(state="MD", employer="").is_usable()

    def test_requires_a_date_or_headcount(self):
        assert not WarnNotice(state="MD", employer="Acme Corp").is_usable()
        assert WarnNotice(state="MD", employer="Acme Corp", employees=10).is_usable()

    def test_dates_serialize_to_iso(self):
        from datetime import date

        notice = WarnNotice(state="MD", employer="Acme", notice_date=date(2024, 1, 2))
        assert notice.as_dict()["notice_date"] == "2024-01-02"
