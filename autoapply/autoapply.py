import re
from collections.abc import Callable
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from autoapply.helper.gotolink import Gotolink


class JobScraper:

    JOB_PATH = re.compile(r"^/jobs/\d+(?:-[^/]+)?$")

    def __init__(self, profile_path, progress_callback: Callable[[str], None] | None = None):
        self.options = FirefoxOptions()

        self.options.add_argument("-profile")
        self.options.add_argument(profile_path)

        self.driver = webdriver.Firefox(
            service=FirefoxService(
                executable_path=GeckoDriverManager().install()
            ),
            options=self.options
        )

        self.driver.get('https://wellfound.com/jobs')

        self.data = []
        self.progress_callback = progress_callback
        self.scrape_warning = ""


    def _report(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def _search_input(self, field_name: str, aliases: tuple[str, ...]):
        selectors = (
            "input:not([type='hidden'])",
            "textarea",
            "[role='textbox']",
            "[role='combobox']",
            "[contenteditable='true']",
        )
        elements = []
        for selector in selectors:
            elements.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))

        visible_descriptions = []
        direct_matches = []
        context_matches = []
        matching_terms = tuple(alias.casefold() for alias in aliases)
        seen = set()
        seen_elements = set()
        for element in elements:
            element_id = getattr(element, "id", None)
            if element_id is not None and element_id in seen_elements:
                continue
            if element_id is not None:
                seen_elements.add(element_id)
            if not element.is_displayed():
                continue
            labels = self.driver.execute_script(
                """
                const node = arguments[0];
                const labels = node.labels ? Array.from(node.labels).map(label => label.innerText) : [];
                const labelledBy = (node.getAttribute("aria-labelledby") || "")
                  .split(/\\s+/).map(id => document.getElementById(id)?.innerText || "");
                const parentLabel = node.closest("label");
                if (parentLabel) labels.push(parentLabel.innerText);
                return {
                  direct: [
                  node.getAttribute("aria-label"),
                  node.getAttribute("placeholder"),
                  node.getAttribute("name"),
                  node.getAttribute("id"),
                  node.getAttribute("data-testid"),
                  ...labelledBy,
                  ...labels,
                  ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim(),
                  context: (node.parentElement?.innerText || "").replace(/\\s+/g, " ").trim()
                };
                """,
                element,
            )
            description = " ".join(str(labels.get("direct") or "").split())
            context = " ".join(str(labels.get("context") or "").split())
            if description and description not in seen:
                visible_descriptions.append(description[:140])
                seen.add(description)
            normalized = description.casefold()
            if any(alias in normalized for alias in matching_terms):
                direct_matches.append((element, normalized))
            context_normalized = context.casefold()
            if (
                len(context) <= 100
                and any(context_normalized == alias or context_normalized.startswith(f"{alias} ") for alias in matching_terms)
            ):
                context_matches.append(element)
        if direct_matches:
            exact = [
                element for element, description in direct_matches
                if any(description == alias for alias in matching_terms)
            ]
            return exact[0] if exact else direct_matches[0][0]
        if len(context_matches) == 1:
            return context_matches[0]
        available = "; ".join(visible_descriptions[:8]) or "no visible text-entry controls"
        if len(context_matches) > 1:
            available = f"{available}; multiple fields have matching nearby labels"
        raise RuntimeError(
            f"Wellfound {field_name} search field was not found. "
            f"Visible fields: {available}"
        )

    @staticmethod
    def _fill_search_field(element, value: str) -> None:
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        if value:
            element.send_keys(value)

    def _open_filter_panel(self) -> None:
        def find_filter_button(_driver):
            for selector in ("button", "[role='button']", "a"):
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if not element.is_displayed():
                        continue
                    label = " ".join(
                        " ".join(
                            str(element.get_attribute(attribute) or "")
                            for attribute in ("aria-label", "title")
                        ).casefold().split()
                    )
                    text = " ".join(element.text.casefold().split())
                    if text in ("filter", "filters") or label in ("filter", "filters"):
                        return element
            return False

        try:
            filter_button = WebDriverWait(self.driver, 15).until(find_filter_button)
        except TimeoutException as error:
            buttons = [
                " ".join(element.text.split())
                for element in self.driver.find_elements(By.CSS_SELECTOR, "button")
                if element.is_displayed() and element.text.strip()
            ]
            visible_buttons = ", ".join(buttons[:8]) or "none"
            raise RuntimeError(
                "Wellfound's Filters button was not found. "
                f"Current page: {self.driver.title!r} ({self.driver.current_url}); "
                f"visible buttons: {visible_buttons}. "
                "Check that Firefox is signed in and the jobs page loaded normally."
            ) from error

        filter_button.click()
        try:
            WebDriverWait(self.driver, 10).until(
                lambda driver: any(
                    element.is_displayed()
                    for element in driver.find_elements(By.CSS_SELECTOR, "input, button, [role='checkbox']")
                )
            )
        except TimeoutException as error:
            raise RuntimeError("Wellfound's Filters panel did not open.") from error

    def _apply_included_keyword(self, keyword: str) -> None:
        if not keyword:
            return
        candidates = self.driver.find_elements(
            By.CSS_SELECTOR,
            "input:not([type='hidden']), textarea, [role='textbox'], [contenteditable='true']",
        )
        for element in candidates:
            if not element.is_displayed():
                continue
            context = self.driver.execute_script(
                """
                let node = arguments[0];
                for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
                  const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
                  const normalized = text.toLowerCase();
                  if (normalized.includes("included keywords") && !normalized.includes("excluded keywords")) {
                    return text;
                  }
                }
                return "";
                """,
                element,
            )
            context = " ".join(str(context or "").casefold().split())
            if "included keywords" not in context or "excluded keywords" in context:
                continue
            self._fill_search_field(element, keyword)
            element.send_keys(Keys.ENTER)
            return
        raise RuntimeError(
            "Could not find the 'Included keywords' field in Wellfound's Filters panel."
        )

    def _submit_filter_panel(self) -> None:
        for selector in ("button", "[role='button']"):
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if not element.is_displayed():
                    continue
                text = " ".join(element.text.casefold().split())
                if (
                    text in ("apply", "apply filters", "show jobs", "show results", "view jobs", "done")
                    or (text.startswith("show ") and ("job" in text or "result" in text))
                ):
                    element.click()
                    return

    def _search_wellfound(self, country: str, keyword: str) -> None:
        self.keyword_search_applied = bool(keyword)
        stage = "opening Wellfound jobs"
        self.driver.get("https://wellfound.com/jobs")
        try:
            if country:
                stage = "setting the country/location"
                try:
                    location_input = self._search_input(
                        "location",
                        ("location", "city", "country"),
                    )
                except RuntimeError as error:
                    if "search field was not found" not in str(error):
                        raise
                    self.driver.get(self._location_url(country))
                else:
                    self._fill_search_field(location_input, country)
                    location_input.send_keys(Keys.ENTER)
            stage = "opening Wellfound's Filters panel"
            self._open_filter_panel()
            stage = "setting the Included keywords filter"
            self._apply_included_keyword(keyword)
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            raise RuntimeError(
                f"Could not configure Wellfound's search filters while {stage}: {detail}"
            ) from error

        self._report("Opened Wellfound filters and set location and included keywords.")

    @staticmethod
    def _location_url(country: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", country.casefold()).strip("-")
        return f"https://wellfound.com/location/{slug}" if slug else "https://wellfound.com/jobs"

    @staticmethod
    def _matches_job_title(title: str, keyword: str) -> bool:
        normalized_keyword = keyword.casefold().strip()
        return not normalized_keyword or normalized_keyword in title.casefold()

    def _toggle_state(self, element) -> bool | None:
        if element.tag_name == "input" and element.get_attribute("type") in ("checkbox", "radio"):
            return element.is_selected()
        associated_state = self.driver.execute_script(
            """
            const control = arguments[0].control;
            return control && (control.type === "checkbox" || control.type === "radio")
              ? control.checked
              : null;
            """,
            element,
        )
        if associated_state is not None:
            return bool(associated_state)
        if element.tag_name == "label":
            inputs = element.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], input[type='radio']")
            if inputs:
                return inputs[0].is_selected()
        for attribute in ("aria-checked", "aria-pressed", "data-state"):
            value = element.get_attribute(attribute)
            if value is not None:
                return value.casefold() in ("true", "checked", "on")
        inputs = element.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], input[type='radio']")
        if inputs:
            return inputs[0].is_selected()
        return None

    def _filter_control(self, label: str):
        normalized = " ".join(label.casefold().split())
        selectors = (
            "label",
            "[role='checkbox']",
            "[role='switch']",
            "[role='option']",
            "button",
            "[role='button']",
            "[role='heading']",
            "h2",
            "h3",
            "summary",
            "div",
            "span",
        )
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and " ".join(element.text.casefold().split()) == normalized:
                    return element
        return None

    def _apply_filter_option(self, section: str, label: str, selected: bool = True) -> None:
        control = self._filter_control(label)
        if control is None:
            heading = self._filter_control(section)
            if heading is not None:
                heading.click()
            control = self._filter_control(label)
        if control is None:
            raise RuntimeError(f"Wellfound filter {label!r} was not found under {section!r}.")

        current = self._toggle_state(control)
        if current is None:
            raise RuntimeError(
                f"Wellfound exposed {label!r}, but its selected state could not be verified."
            )
        if current != selected:
            control.click()
            control = self._filter_control(label)
            if control is None:
                raise RuntimeError(f"Wellfound filter {label!r} disappeared after selection.")
            current = self._toggle_state(control)
        if current != selected:
            raise RuntimeError(f"Wellfound did not apply the {label!r} filter.")

    def _set_filter_group(self, section: str, labels: list[str], selected_label: str = "") -> None:
        opened_section = False
        for label in labels:
            control = self._filter_control(label)
            if control is None and not opened_section:
                heading = self._filter_control(section)
                if heading is not None:
                    heading.click()
                    opened_section = True
            control = self._filter_control(label)
            if control is None:
                raise RuntimeError(f"Wellfound filter {label!r} was not found under {section!r}.")
            current = self._toggle_state(control)
            if current is None:
                raise RuntimeError(
                    f"Wellfound exposed {label!r}, but its selected state could not be verified."
                )
            should_select = label == selected_label
            if current != should_select:
                control.click()
                control = self._filter_control(label)
                if control is None:
                    raise RuntimeError(f"Wellfound filter {label!r} disappeared after selection.")
                current = self._toggle_state(control)
            if current != should_select:
                raise RuntimeError(f"Wellfound did not update the {label!r} filter.")

    def _apply_wellfound_filters(self, filters: dict[str, str]) -> None:
        employment_types = {
            "Full-time": "Full Time",
            "Contract": "Contract",
            "Internship": "Internship",
            "Cofounder": "Cofounder",
        }
        selected_type = filters.get("employment_type", "")
        if selected_type and selected_type not in employment_types:
            raise RuntimeError(
                f"{selected_type!r} is not an employment type available on Wellfound. "
                "Choose Full Time, Contract, Internship, or Cofounder."
            )
        if self._filter_control("Full Time") is None and self._filter_control("Job Types") is None:
            job_details = self._filter_control("Job Details")
            if job_details is not None:
                job_details.click()
        self._set_filter_group(
            "Job Types",
            list(employment_types.values()),
            employment_types.get(selected_type, ""),
        )

        company_size = filters.get("company_size", "")
        size_labels = {
            "1-10": "1-10 employees",
            "11-50": "11-50 employees",
            "51-200": "51-200 employees",
            "201-500": "201-500 employees",
            "501-1000": "501-1000 employees",
            "1001-5000": "1001-5000 employees",
            "5000+": "5000+ employees",
        }
        if company_size and company_size not in size_labels:
            raise RuntimeError(f"Unsupported Wellfound company size: {company_size!r}.")
        self._set_filter_group(
            "Company size",
            list(size_labels.values()),
            size_labels.get(company_size, ""),
        )

        self._apply_filter_option(
            "Remote culture",
            "Only show jobs at companies that are mostly or fully remote",
            selected=filters.get("remote_only") == "true",
        )

    def _job_links(self) -> list[tuple[str, str]]:
        """Find job URLs and their own visible link text."""
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/jobs/']"))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "Wellfound did not expose job links. Confirm that Firefox is signed in and the jobs page loaded."
            ) from error

        links, seen = [], set()
        for element in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']"):
            href = element.get_attribute("href")
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.netloc.endswith("wellfound.com") and self.JOB_PATH.fullmatch(parsed.path) and href not in seen:
                seen.add(href)
                title = element.text.strip()
                links.append((href, title))
        if not links:
            raise RuntimeError("Found the jobs page, but no individual job listings were available.")
        return links

    def _job_details(self, link: str) -> dict[str, str]:
        self.driver.get(link)
        title = WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
        ).text.strip()
        page_text = self.driver.find_element(By.TAG_NAME, "body").text
        company = "Company unavailable"
        company_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/company/']")
        if company_links and company_links[0].text.strip():
            company = company_links[0].text.strip()

        description = page_text.split("About the job", 1)[-1]
        description = description.split("About the company", 1)[0]
        description = " ".join(description.split())
        header = page_text.split("About the job", 1)[0]
        location, compensation = "Location unavailable", "Compensation unavailable"
        for line in (line.strip("•| ") for line in header.splitlines()):
            if any(symbol in line for symbol in ("$", "₹", "€", "£")):
                compensation = line
            if "remote" in line.lower() or "office" in line.lower():
                location = line

        employee_match = re.search(
            r"\b(1-10|11-50|51-200|201-500|501-1000|1001-5000|5001\+)\s+Employees\b",
            header,
            re.IGNORECASE,
        )
        return {"company": company, "role": title or "Untitled role", "location": location,
                "stipend": compensation, "description": description or "Description unavailable.",
                "employee_count": employee_match.group(1) if employee_match else "Not listed",
                "page_text": page_text, "link": link}

    def scrape_jobs(self, maximum_jobs: int = 30, filters: dict[str, str] | None = None):
        """Import a bounded set of roles and their descriptions for dashboard review."""
        filters = filters or {}
        self._search_wellfound(filters.get("country", ""), filters.get("keyword", ""))
        self._apply_wellfound_filters(filters)
        self._submit_filter_panel()
        try:
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/jobs/']"))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "Wellfound did not show job results after applying the selected filters."
            ) from error
        candidate_limit = min(max(maximum_jobs * 5, 30), 100)
        listings = self._job_links()[:candidate_limit]
        keyword = filters.get("keyword", "")
        candidates = [
            (link, title) for link, title in listings
            if self.keyword_search_applied or not title or self._matches_job_title(title, keyword)
        ]
        self._report(
            f"Wellfound filters are set. Checking {len(candidates)} candidate listings…"
        )
        self.data = []
        failures = []
        filtered_out = len(listings) - len(candidates)
        for index, (link, _title) in enumerate(candidates, start=1):
            self._report(f"Checking job {index} of {len(candidates)} ({len(self.data)} imported)…")
            try:
                job = self._job_details(link)
                if not self._matches_job_title(job["role"], keyword):
                    filtered_out += 1
                    continue
                job.pop("page_text", None)
                self.data.append(job)
                if len(self.data) >= maximum_jobs:
                    break
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
        if not self.data:
            if failures:
                details = "; ".join(failures[:3])
                raise RuntimeError(
                    f"No jobs were imported. {len(failures)} listing page(s) failed to load. "
                    f"First error(s): {details}"
                )
            if filtered_out:
                raise RuntimeError(
                    f"No jobs matched the selected filters ({filtered_out} listing(s) checked). "
                    "Broaden the filters and try again."
                )
            raise RuntimeError("Wellfound returned no job listings for the selected filters.")
        if failures:
            self.scrape_warning = f" Skipped {len(failures)} listing page(s) that could not be read."

    def run_gui(self):
        raise RuntimeError("The Tkinter selector was replaced by the local web dashboard.")

    def process_links(self):
        # Firefox cannot reliably open the same profile in two live sessions.
        self.close()
        gotolink = Gotolink()
        gotolink.process_links(d=self.data)

    def close(self):
        if getattr(self, "driver", None):
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None
