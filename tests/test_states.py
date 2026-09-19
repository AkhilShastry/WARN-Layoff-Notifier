"""Tests for the custom state handlers.

These pin down bugs that actually shipped and were caught by the field-coverage
readout in `warn inspect`, not by anything crashing.
"""

import re

import pytest

from warn.parsers.tables import make_soup
from warn.states.pennsylvania import _first_int, _notice_context, _panel_fields
from warn.states.pdf_links import _clean_employer, _date_from_path


class TestPennsylvaniaPanel:
    def test_splits_labelled_fields(self):
        text = ("150 Cesanek Road, Northampton, PA 18067 COUNTY: Northampton "
                "# AFFECTED: 52 EFFECTIVE DATE: beginning 11/16/2026 "
                "CLOSURE OR LAYOFF: Closure")
        fields = _panel_fields(text)
        assert fields["county"] == "Northampton"
        assert fields["affected"] == "52"
        assert fields["closureorlayoff"] == "Closure"
        assert "150 Cesanek Road" in fields["_address"]

    def test_plural_effective_dates_label(self):
        """The page uses both "EFFECTIVE DATE:" and "EFFECTIVE DATES:".

        Missing the plural let the date text run into the headcount, so a
        129-person layoff was recorded as 20,223 from "7/3/20223".
        """
        text = ("1750 Power Plant Road Homer City, PA 15748 COUNTY: Indiana "
                "# AFFECTED: 129 EFFECTIVE DATES: 7/3/20223 - 10/16/2023 "
                "CLOSURE OR LAYOFF: Closure")
        fields = _panel_fields(text)
        assert fields["affected"] == "129"
        assert _first_int(fields["affected"]) == 129

    def test_first_int_ignores_trailing_text(self):
        assert _first_int("129 EFFECTIVE DATES: 7/3/20223") == 129
        assert _first_int(None) is None
        assert _first_int("unknown") is None


class TestPennsylvaniaContext:
    # The year is a sibling heading; the month is an ancestor accordion item.
    MARKUP = """
    <div>
      <h2>2026</h2>
      <div class="cmp-accordion__item">
        <h3 class="cmp-accordion__title">September</h3>
        <div class="cmp-accordion__panel">
          <div class="wrapper"><div class="wrapper">
            <div class="cmp-accordion__item" id="leaf">
              <h3 class="cmp-accordion__title">Kenco Logistic Services, LLC</h3>
              <div class="cmp-accordion__panel">COUNTY: Northampton # AFFECTED: 52</div>
            </div>
          </div></div>
        </div>
      </div>
    </div>
    """

    def test_finds_year_and_month(self):
        soup = make_soup(self.MARKUP)
        leaf = soup.find(id="leaf")
        assert _notice_context(leaf) == (2026, 9)

    def test_year_is_found_even_when_deeply_nested(self):
        """The year heading is not an ancestor, so a parent-walk misses it and
        every notice ends up undated."""
        soup = make_soup(self.MARKUP)
        leaf = soup.find(id="leaf")
        year, _ = _notice_context(leaf)
        assert year is not None


class TestPdfLinkParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("ECM Energy Services WARN 8-19-26", "ECM Energy Services"),
            ("Greenbrier Minerals WARN 2-13-26", "Greenbrier Minerals"),
            ("WARN Notice - Felman Productions", "Felman Productions"),
        ],
    )
    def test_employer_recovered_from_link_text(self, text, expected):
        assert _clean_employer(text) == expected

    def test_date_from_upload_path(self):
        from datetime import date

        got = _date_from_path("/wp-content/uploads/2026/08/ECM-Energy.pdf")
        assert got == date(2026, 8, 1)

    def test_nonsense_month_falls_back_to_the_year(self):
        """An unusable month segment shouldn't discard the year we do know."""
        from datetime import date

        assert _date_from_path("/uploads/2026/99/x.pdf") == date(2026, 1, 1)

    def test_no_date_in_path(self):
        assert _date_from_path("/uploads/notices/acme.pdf") is None


class TestNevadaRow:
    def test_middle_columns_rejoin_and_split(self):
        from warn.states.nevada import _MIDDLE

        # Columns come out of the PDF smeared: '3/15/2026La' + 'yoff' + '1Spirit...'
        middle = "".join(["3/15/2026La", "yoff", "1Spirit Airli", "ne", "s"])
        match = _MIDDLE.match(middle)
        assert match is not None
        effective, action, count, employer = match.groups()
        assert effective == "3/15/2026"
        assert action.lower() == "layoff"
        assert count == "1"
        assert employer == "Spirit Airlines"

    def test_handles_unknown_count(self):
        from warn.states.nevada import _MIDDLE

        match = _MIDDLE.match("4/26/2026Closure unknownDig It Coffee Co")
        assert match is not None
        assert match.group(3).lower() == "unknown"
