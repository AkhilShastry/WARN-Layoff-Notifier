"""Where each state publishes its WARN notices.

One `Source` per state. Strategy `auto` is the default and is deliberately
forgiving: it sniffs the payload, scrapes an inline table if there is one, and
otherwise falls back to hunting for linked data files. States needing more than
that carry an explicit strategy or a custom handler in `warn.states`.

Run `python -m warn doctor` to re-verify every entry in this file; state sites
change their URLs roughly annually.
"""

from __future__ import annotations

from .source import Source

# Google Sheets published as a spreadsheet: request the CSV export instead of
# the JS editor UI.
_CO_SHEET = "19jmo4Cwj933cmSBKV1t0zZ5O-2H5IpiLIhSH9MF8WF0"

SOURCES: list[Source] = [
    Source(
        state="AL", name="Alabama Dept. of Commerce",
        url="https://www.madeinalabama.com/warn-list/",
        strategy="auto",
    ),
    Source(
        state="AK", name="Alaska DOLWD",
        url="https://jobs.alaska.gov/rr/WARN_notices.htm",
        strategy="html", merge_all_tables=True,
    ),
    Source(
        state="AZ", name="Arizona @ Work (AJC)",
        url="https://www.azjobconnection.gov/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="AR", name="Arkansas Division of Workforce Services",
        url="https://dws.arkansas.gov/workforce-services/employers/dislocated-worker-services/",
        strategy="auto",
        unavailable="Arkansas does not publish WARN notices publicly; they are "
                    "confidential under A.C.A. 11-10-314. DWS posts only "
                    "guidance documents.",
    ),
    Source(
        state="CA", name="California EDD",
        url="https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx",
        strategy="file",
        notes="EDD publishes one workbook per program year",
    ),
    Source(
        state="CO", name="Colorado DLE",
        url=f"https://docs.google.com/spreadsheets/d/{_CO_SHEET}/export?format=csv&gid=1928499704",
        strategy="file", notes="State publishes via Google Sheets",
    ),
    Source(
        state="CT", name="Connecticut DOL",
        url="https://www.ctdol.state.ct.us/progsupt/bussrvce/warnreports/warnreports.htm",
        strategy="landing", verify=False,
        notes="Server presents an incomplete certificate chain",
    ),
    Source(
        state="DE", name="Delaware JobLink",
        url="https://joblink.delaware.gov/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="FL", name="Florida DEO (RE ACT)",
        url="https://reactwarn.floridajobs.org/WarnList/Records",
        strategy="custom", notes="Paginated ASP.NET list with CSV export",
    ),
    Source(
        state="GA", name="Georgia TCSG",
        url="https://www.tcsg.edu/warn-public-view/",
        strategy="custom", notes="GravityView DataTables grid over WP AJAX",
    ),
    Source(
        state="HI", name="Hawaii DLIR",
        url="https://labor.hawaii.gov/wdc/real-time-warn-updates/",
        strategy="custom", notes="Page of per-notice PDF links, no table",
    ),
    Source(
        state="ID", name="Idaho Dept. of Labor",
        url="https://www.labor.idaho.gov/businesses/layoff-assistance/",
        strategy="auto",
    ),
    Source(
        state="IL", name="Illinois workNet / DCEO",
        url="https://www.illinoisworknet.com/LayoffRecovery/Pages/ArchivedWARNReports.aspx",
        strategy="landing", max_files=18, link_filter="warn",
        notes="Monthly XLSX workbooks behind SharePoint download links",
    ),
    Source(
        state="IN", name="Indiana DWD",
        url="https://www.in.gov/dwd/warn-notices/current-warn-notices",
        strategy="auto", notes="Landing page has no table; this sub-page does",
    ),
    Source(
        state="IA", name="Iowa Workforce Development",
        url="https://workforce.iowa.gov/media/1190/download?inline",
        strategy="file", notes="The published 'WARN Log' workbook",
    ),
    Source(
        state="KS", name="KansasWorks",
        url="https://www.kansasworks.com/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="KY", name="Kentucky Career Center",
        url="https://kcc.ky.gov/Pages/News.aspx",
        strategy="landing", link_filter="warn report", max_files=4,
        notes="Dated XLSX reports linked from the news page",
    ),
    Source(
        state="LA", name="Louisiana Workforce Commission",
        url="https://www.laworks.net/Downloads/WARN/",
        strategy="landing",
    ),
    Source(
        state="ME", name="Maine JobLink",
        url="https://joblink.maine.gov/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="MD", name="Maryland Dept. of Labor",
        url="https://labor.maryland.gov/employment/warn.shtml",
        strategy="auto",
    ),
    Source(
        state="MA", name="Massachusetts EOLWD",
        url="https://www.mass.gov/info-details/worker-adjustment-and-retraining-"
            "notification-act-warn-layoff-and-closure-updates",
        strategy="landing", link_filter="warn", max_files=6,
        notes="One XLSX per fiscal year, plus a weekly CSV",
    ),
    Source(
        state="MI", name="Michigan LEO",
        url="https://www.michigan.gov/leo/bureaus-agencies/wd/warn-notices",
        strategy="auto",
    ),
    Source(
        state="MN", name="Minnesota DEED",
        url="https://mn.gov/deed/business/layoff-resources/warn-archive/",
        strategy="custom", impersonate=True,
        notes="Bot filter needs browser impersonation; page is PDF links only",
    ),
    Source(
        state="MS", name="Mississippi MDES",
        url="https://mdes.ms.gov/information-center/warn-information/",
        strategy="auto",
    ),
    Source(
        state="MO", name="Missouri DHEWD",
        url="https://jobs.mo.gov/warn/2026",
        year_template="https://jobs.mo.gov/warn/{year}",
        strategy="html", impersonate=True,
        overrides={"title": "employer"},
        notes="Incapsula bot filter; 'Title' column holds the employer",
    ),
    Source(
        state="MT", name="Montana DLI",
        url="https://wsd.dli.mt.gov/wioa/related-links/warn-notice-page",
        strategy="auto",
    ),
    Source(
        state="NE", name="Nebraska DOL",
        url="https://dol.nebraska.gov/ReemploymentServices/LayoffServices/LayoffsAndDownsizingWARN",
        strategy="auto",
    ),
    Source(
        state="NV", name="Nevada DETR",
        url="https://detr.nv.gov/Page/WARN",
        strategy="custom", max_files=6,
        notes="Annual 'WARN and Non-WARN Master' PDF with no ruled table",
    ),
    Source(
        state="NH", name="New Hampshire Employment Security",
        url="https://www.nhes.nh.gov/nhworks/warn/index.htm",
        strategy="auto",
    ),
    Source(
        state="NJ", name="New Jersey DOL",
        url="https://www.nj.gov/labor/assets/PDFs/WARN/2026_WARN_Notice_Archive.pdf",
        strategy="custom", notes="One PDF archive per year, no header row",
    ),
    Source(
        state="NM", name="New Mexico DWS",
        url="https://www.dws.state.nm.us/Rapid-Response",
        strategy="auto",
    ),
    Source(
        state="NY", name="New York DOL",
        url="https://dol.ny.gov/warn-notices",
        strategy="custom",
        notes="The dashboard is Tableau; the per-year archive pages are HTML",
    ),
    Source(
        state="NC", name="North Carolina Commerce",
        url="https://www.commerce.nc.gov/data-tools-reports/labor-market-data-tools/"
            "workforce-warn-reports/report-workforce-warn-summary-list-2026",
        year_template="https://www.commerce.nc.gov/data-tools-reports/"
                      "labor-market-data-tools/workforce-warn-reports/"
                      "report-workforce-warn-summary-list-{year}",
        strategy="html", notes="One summary-list page per year",
    ),
    Source(
        state="ND", name="Job Service North Dakota",
        url="https://www.jobsnd.com/warn-notices",
        strategy="auto",
    ),
    Source(
        state="OH", name="Ohio JFS",
        url="https://jfs.ohio.gov/job-workforce-services/job-programs-and-services/"
            "submit-a-warn-notice/current-public-notices-of-layoffs-and-closures",
        strategy="custom", notes="Next.js page; the data is a CSV on Ohio's CDN",
    ),
    Source(
        state="OK", name="OKJobMatch",
        url="https://okjobmatch.com/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="OR", name="Oregon Rapid Response (HECC)",
        url="https://ccwd.hecc.oregon.gov/Layoff/WARN",
        strategy="custom", notes="Paginated layoff-tracking table",
    ),
    Source(
        state="PA", name="Pennsylvania L&I",
        url="https://www.pa.gov/agencies/dli/programs-services/workforce-development-home/"
            "warn-requirements/warn-notices",
        strategy="custom", notes="Nested accordion panels, no table",
    ),
    Source(
        state="RI", name="Rhode Island DLT",
        url="https://dlt.ri.gov/employers/worker-adjustment-and-retraining-notification-warn",
        strategy="auto",
    ),
    Source(
        state="SC", name="SC Works / DEW",
        url="https://scworks.org/employer/employer-programs/"
            "worker-adjustment-and-retraining-notification-warn-act",
        strategy="landing", link_filter="warn_report", max_files=3,
        notes="Cumulative year-to-date PDF report, re-issued under a new "
              "dated filename every few weeks",
    ),
    Source(
        state="SD", name="South Dakota DLR",
        url="https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx",
        strategy="auto",
    ),
    Source(
        state="TN", name="Tennessee DLWD",
        url="https://www.tn.gov/workforce/general-resources/major-publications0/"
            "major-publications-redirect/reports.html",
        strategy="auto",
    ),
    Source(
        state="TX", name="Texas Workforce Commission (open data)",
        url="https://data.texas.gov/resource/8w53-c4f6.json?$limit=50000",
        strategy="json",
        notes="TWC's own site is bot-filtered; the Socrata dataset is the "
              "same data, complete and machine-readable",
    ),
    Source(
        state="UT", name="Utah DWS",
        url="https://jobs.utah.gov/employer/business/warnnotices.html",
        strategy="auto",
    ),
    Source(
        state="VT", name="Vermont JobLink",
        url="https://www.vermontjoblink.com/search/warn_lookups",
        strategy="custom", notes="Geographic Solutions VOS search app",
    ),
    Source(
        state="VA", name="Virginia Works",
        url="https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/",
        strategy="auto", notes="vec.virginia.gov now redirects here",
    ),
    Source(
        state="WA", name="Washington ESD",
        url="https://fortress.wa.gov/esd/file/WARN/Public/SearchWARN.aspx",
        strategy="custom", notes="ASP.NET GridView with postback paging",
    ),
    Source(
        state="WV", name="WorkForce West Virginia",
        url="https://workforcewv.org/businesses/layoffs-downsizing/warn-listing/",
        strategy="custom", notes="Only per-notice PDF links; parsed from link text",
    ),
    Source(
        state="WI", name="Wisconsin DWD",
        url="https://docs.google.com/spreadsheets/d/"
            "1cyZiHZcepBI7ShB3dMcRprUFRG24lbwEnEDRBMhAqsA/gviz/tq?tqx=out:csv",
        strategy="file",
        notes="DWD's page is a JS client over this Google Sheet; read the "
              "sheet directly to avoid needing their API key",
    ),
    Source(
        state="WY", name="Wyoming DWS",
        url="https://dws.wyo.gov/",
        strategy="auto",
        unavailable="Wyoming does not publish WARN notices publicly; they are "
                    "confidential under Wyo. Stat. 9-2-2607. Records are "
                    "available only on request from DWS.",
    ),
]

# Import custom handlers so their @handler registrations run.
from . import states as _states  # noqa: E402,F401  (side-effect import)

BY_STATE: dict[str, Source] = {s.state.upper(): s for s in SOURCES}


def get_sources(states: list[str] | None = None) -> list[Source]:
    """Select sources by USPS code; None means all of them."""
    if not states:
        return list(SOURCES)
    wanted = {s.strip().upper() for s in states if s.strip()}
    unknown = wanted - set(BY_STATE)
    if unknown:
        raise KeyError(f"unknown state code(s): {', '.join(sorted(unknown))}")
    return [BY_STATE[code] for code in sorted(wanted)]
