/**
 * Search page interactivity.
 *
 * The page works without this file: `#search-form` is a plain GET form that
 * submits to `/` and gets a full server-rendered page back. This script
 * progressively enhances that: it intercepts form input, fetches
 * `/api/search` instead, and swaps the results table in place, so searching
 * feels instant and never reloads the page. If a request fails or the script
 * doesn't load, the plain form still works.
 *
 * No build step, no framework, no dependencies -- just the DOM.
 */
(function () {
  "use strict";

  const form = document.getElementById("search-form");
  const tbody = document.getElementById("results-body");
  if (!form || !tbody) return; // not on the search page

  const resultsCard = document.getElementById("results-card");
  const emptyState = document.getElementById("empty-state");
  const resultsHeading = document.getElementById("results-heading");
  const resultsCount = document.getElementById("results-count");
  const searchStatus = document.getElementById("search-status");
  const exportLink = document.getElementById("export-link");
  const pagination = document.getElementById("pagination");
  const activeOnlyBox = document.getElementById("active_only");
  const clearLink = document.getElementById("clear-filters");

  const FILTER_IDS = ["q", "state", "city", "min_employees", "since"];

  // ---- helpers ----------------------------------------------------------

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function badgeClass(noticeType) {
    return noticeType && noticeType.indexOf("Closure") !== -1 ? "closure" : "layoff";
  }

  function formatWorkers(n) {
    return n == null ? "—" : n.toLocaleString();
  }

  function currentParams(page) {
    const params = new URLSearchParams(new FormData(form));
    if (activeOnlyBox && activeOnlyBox.checked) params.set("active_only", "1");
    // FormData won't include an unchecked checkbox at all, which is what we
    // want -- but also strip anything that ended up empty so the URL stays
    // clean (e.g. "?q=&state=" instead of just "?").
    for (const key of Array.from(params.keys())) {
      if (params.get(key) === "") params.delete(key);
    }
    params.set("page", String(page || 1));
    return params;
  }

  function hasActiveFilters(params) {
    return FILTER_IDS.some((id) => params.get(id)) || params.get("active_only") === "1";
  }

  // ---- rendering ----------------------------------------------------------

  function renderRows(results) {
    if (!results.length) {
      tbody.innerHTML = "";
      resultsCard.hidden = true;
      emptyState.hidden = false;
      return;
    }
    resultsCard.hidden = false;
    emptyState.hidden = true;

    tbody.innerHTML = results
      .map(function (n) {
        const location = [n.city, n.state].filter(Boolean).join(", ");
        const county = n.county
          ? '<div class="muted">' + escapeHtml(n.county) + " County</div>"
          : "";
        const industry = n.industry
          ? '<div class="muted">' + escapeHtml(n.industry) + "</div>"
          : "";
        const badge = n.notice_type
          ? '<span class="badge ' + badgeClass(n.notice_type) + '">' +
            escapeHtml(n.notice_type) + "</span>"
          : "";
        const source = n.source_url
          ? '<a href="' + escapeHtml(n.source_url) +
            '" target="_blank" rel="noopener">View filing &#8599;</a>'
          : "";

        return (
          "<tr>" +
          '<td><div class="employer">' + escapeHtml(n.employer) + "</div>" + industry + "</td>" +
          "<td>" + escapeHtml(location) + county + "</td>" +
          "<td>" + (n.notice_date || "—") + "</td>" +
          "<td>" + (n.effective_date || "—") + "</td>" +
          '<td class="num-cell">' + formatWorkers(n.employees) + "</td>" +
          "<td>" + badge + "</td>" +
          '<td class="source-link">' + source + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function renderPagination(page, totalPages) {
    if (totalPages <= 1) {
      pagination.innerHTML = "";
      return;
    }
    const prev = page > 1
      ? '<a href="#" data-page="' + (page - 1) + '">&larr; Prev</a>'
      : '<span class="disabled">&larr; Prev</span>';
    const next = page < totalPages
      ? '<a href="#" data-page="' + (page + 1) + '">Next &rarr;</a>'
      : '<span class="disabled">Next &rarr;</span>';
    pagination.innerHTML =
      prev + '<span class="current">Page ' + page + " of " + totalPages + "</span>" + next;
  }

  // ---- the actual search ----------------------------------------------------------

  let inFlight = null;

  function runSearch(page) {
    const params = currentParams(page);

    if (searchStatus) searchStatus.hidden = false;
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;

    fetch("/api/search?" + params.toString(), { signal: controller.signal })
      .then(function (response) {
        if (!response.ok) throw new Error("search request failed");
        return response.json();
      })
      .then(function (data) {
        renderRows(data.results);
        renderPagination(data.page, data.total_pages);
        resultsHeading.textContent = hasActiveFilters(params) ? "Search results" : "Latest notices";
        resultsCount.textContent =
          data.total.toLocaleString() + " match" + (data.total === 1 ? "" : "es");
        if (exportLink) exportLink.href = "/export.csv?" + params.toString();
        if (searchStatus) searchStatus.hidden = true;

        // Keep the URL (and therefore the back button and bookmarking) in
        // sync without triggering a navigation.
        const url = new URL(window.location.href);
        url.search = params.toString();
        window.history.pushState({ page: data.page }, "", url);
      })
      .catch(function (err) {
        if (err.name === "AbortError") return; // superseded by a newer search
        if (searchStatus) searchStatus.hidden = true;
        tbody.innerHTML =
          '<tr><td colspan="7" class="muted">Something went wrong loading results. ' +
          "Try again, or reload the page.</td></tr>";
        resultsCard.hidden = false;
        emptyState.hidden = true;
      });
  }

  // ---- wiring ----------------------------------------------------------

  let debounceTimer = null;
  function debouncedSearch() {
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(function () {
      runSearch(1);
    }, 300);
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    window.clearTimeout(debounceTimer);
    runSearch(1);
  });

  // Free-text fields: search as you type, debounced so we're not firing a
  // request on every keystroke.
  ["q", "city", "min_employees"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", debouncedSearch);
  });

  // Discrete controls: search immediately on change, no debounce needed.
  ["state", "since"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", function () { runSearch(1); });
  });

  if (activeOnlyBox) {
    activeOnlyBox.addEventListener("change", function () { runSearch(1); });
  }

  pagination.addEventListener("click", function (event) {
    const link = event.target.closest("a[data-page]");
    if (!link) return;
    event.preventDefault();
    runSearch(Number(link.dataset.page));
    window.scrollTo({ top: resultsCard.offsetTop - 20, behavior: "smooth" });
  });

  if (clearLink) {
    clearLink.addEventListener("click", function (event) {
      event.preventDefault();
      form.reset();
      if (activeOnlyBox) activeOnlyBox.checked = false;
      runSearch(1);
    });
  }

  // Support the browser's Back/Forward buttons: re-run the search for
  // whatever the URL says rather than reloading the whole page.
  window.addEventListener("popstate", function () {
    const params = new URLSearchParams(window.location.search);
    FILTER_IDS.forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.value = params.get(id) || "";
    });
    if (activeOnlyBox) activeOnlyBox.checked = params.get("active_only") === "1";
    runSearch(Number(params.get("page")) || 1);
  });
})();
