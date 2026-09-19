"""Tests for HTML table extraction and the rows -> notices funnel."""

from datetime import date

from warn.parsers.records import rows_to_notices
from warn.parsers.tables import best_table, extract_tables

SIMPLE = """
<html><body>
<table>
  <tr><th>Company</th><th>City</th><th>Notice Date</th>
      <th>Layoff Date</th><th>Employees Affected</th></tr>
  <tr><td>Acme Corp</td><td>Baltimore</td><td>1/15/2024</td>
      <td>3/15/2024</td><td>150</td></tr>
  <tr><td>Beta LLC</td><td>Rockville</td><td>2/1/2024</td>
      <td>4/1/2024</td><td>75</td></tr>
</table>
</body></html>
"""

# One employer spanning two worksite rows -- common in state WARN tables.
ROWSPAN = """
<table>
  <tr><th>Company</th><th>City</th><th>Notice Date</th><th>Employees</th></tr>
  <tr><td rowspan="2">Acme Corp</td><td>Baltimore</td><td>1/15/2024</td><td>100</td></tr>
  <tr><td>Rockville</td><td>1/15/2024</td><td>50</td></tr>
</table>
"""

# The real table sits below a title row, and a nav table precedes it.
NOISY = """
<table><tr><td>Home</td><td>About</td></tr><tr><td>Contact</td><td>Help</td></tr></table>
<table>
  <tr><td colspan="4">2024 WARN Notices</td></tr>
  <tr><th>Employer</th><th>City</th><th>WARN Date</th><th># Affected</th></tr>
  <tr><td>Gamma Inc</td><td>Annapolis</td><td>5/1/2024</td><td>30</td></tr>
</table>
"""


class TestTableExtraction:
    def test_reads_headers_and_rows(self):
        table = best_table(SIMPLE)
        assert table is not None
        assert len(table.rows) == 2
        assert table.rows[0]["Company"] == "Acme Corp"

    def test_expands_rowspan(self):
        table = best_table(ROWSPAN)
        assert table is not None
        assert len(table.rows) == 2
        # The spanned employer must appear on both rows.
        assert all(row["Company"] == "Acme Corp" for row in table.rows)
        assert table.rows[1]["City"] == "Rockville"

    def test_picks_the_warn_table_over_navigation(self):
        table = best_table(NOISY)
        assert table is not None
        assert table.rows[0]["Employer"] == "Gamma Inc"

    def test_skips_title_row_above_the_header(self):
        table = best_table(NOISY)
        assert "Employer" in table.headers

    def test_navigation_table_alone_scores_zero(self):
        tables = extract_tables(
            "<table><tr><td>Home</td><td>About</td></tr>"
            "<tr><td>Contact</td><td>Help</td></tr></table>"
        )
        assert all(t.score == 0 for t in tables)

    def test_captures_row_links(self):
        markup = """
        <table><tr><th>Company</th><th>Notice Date</th></tr>
        <tr><td><a href="/notices/acme.pdf">Acme Corp</a></td><td>1/2/2024</td></tr>
        </table>"""
        table = best_table(markup)
        assert table.rows[0]["_link"] == "/notices/acme.pdf"


class TestRowsToNotices:
    def _notices(self, markup):
        table = best_table(markup)
        return rows_to_notices(
            table.rows, state="MD", source_name="test", source_url="http://x"
        )

    def test_builds_typed_records(self):
        notices = self._notices(SIMPLE)
        assert len(notices) == 2
        first = notices[0]
        assert first.employer == "Acme Corp"
        assert first.city == "Baltimore"
        assert first.notice_date == date(2024, 1, 15)
        assert first.effective_date == date(2024, 3, 15)
        assert first.employees == 150
        assert first.state == "MD"

    def test_drops_total_rows(self):
        markup = SIMPLE.replace(
            "</table>",
            "<tr><td>TOTAL</td><td></td><td></td><td></td><td>225</td></tr></table>",
        )
        employers = [n.employer for n in self._notices(markup)]
        assert "TOTAL" not in employers

    def test_drops_repeated_header_rows(self):
        markup = SIMPLE.replace(
            "</table>",
            "<tr><td>Company</td><td>City</td><td>Notice Date</td>"
            "<td>Layoff Date</td><td>Employees Affected</td></tr></table>",
        )
        employers = [n.employer for n in self._notices(markup)]
        assert "Company" not in employers

    def test_returns_nothing_without_an_employer_column(self):
        markup = """<table><tr><th>Widget</th><th>Gizmo</th></tr>
                    <tr><td>1</td><td>2</td></tr></table>"""
        table = best_table(markup)
        rows = table.rows if table else [{"Widget": "1", "Gizmo": "2"}]
        assert rows_to_notices(
            rows, state="MD", source_name="t", source_url="u"
        ) == []

    def test_full_address_in_city_column_is_not_mislabeled(self):
        """Maryland's 'Location' column flattens street + city + state + zip
        into one string with no delimiter between street and city. The whole
        thing should land in `address`, not be shown as if it were a city."""
        markup = """<table>
            <tr><th>Company</th><th>Location</th><th>Notice Date</th></tr>
            <tr><td>Acme</td><td>2611 Pepsi Place Hyattsville, MD 20781</td>
                <td>1/2/2024</td></tr></table>"""
        notices = self._notices(markup)
        assert notices[0].city is None
        assert notices[0].address == "2611 Pepsi Place Hyattsville, MD 20781"
        assert notices[0].zip_code == "20781"

    def test_bare_classification_code_is_not_shown_as_industry(self):
        markup = """<table>
            <tr><th>Company</th><th>NAICS Code</th><th>Notice Date</th></tr>
            <tr><td>Acme</td><td>621498</td><td>1/2/2024</td></tr></table>"""
        notices = self._notices(markup)
        assert notices[0].industry is None

    def test_splits_city_state(self):
        markup = """<table>
            <tr><th>Company</th><th>Location</th><th>Notice Date</th></tr>
            <tr><td>Acme</td><td>Baltimore, MD</td><td>1/2/2024</td></tr></table>"""
        notices = self._notices(markup)
        assert notices[0].city == "Baltimore"
