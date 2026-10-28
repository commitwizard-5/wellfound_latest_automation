import re
from collections.abc import Callable
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
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

    def _job_links(self) -> list[tuple[str, str, str]]:
        """Find job URLs and visible card text without relying on hashed CSS classes."""
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
                card_text = self.driver.execute_script(
                    """
                    let node = arguments[0];
                    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
                      const text = (node.innerText || "").trim();
                      if (text.length >= 30 && text.length <= 1800) return text;
                    }
                    return arguments[0].innerText || "";
                    """,
                    element,
                )
                links.append((href, title, card_text))
        if not links:
            raise RuntimeError("Found the jobs page, but no individual job listings were available.")
        return links

    @staticmethod
    def _matches_listing_filters(title: str, card_text: str, filters: dict[str, str]) -> bool:
        """Reject only candidates the visible result card clearly cannot match."""
        keyword = filters.get("keyword", "").casefold().strip()
        title = title.casefold().strip()
        if keyword and title and keyword not in title:
            return False

        text = card_text.casefold()
        if filters.get("remote_only") == "true":
            onsite_terms = ("in office", "in-office", "onsite", "on-site")
            if not any(term in text for term in ("remote",)) and any(term in text for term in onsite_terms):
                return False

        company_size = filters.get("company_size", "")
        if company_size:
            listed_sizes = re.findall(
                r"\b(1-10|11-50|51-200|201-500|501-1000|1001-5000|5001\+)\s+employees\b",
                text,
            )
            if listed_sizes and company_size not in listed_sizes:
                return False

        employment_type = filters.get("employment_type", "").casefold().replace("-", " ")
        if employment_type:
            listed_types = {
                match.replace("-", " ").replace(" ", "")
                for match in re.findall(r"\b(full[- ]time|internship|part[- ]time)\b", text)
            }
            requested_type = employment_type.replace(" ", "")
            if listed_types and requested_type not in listed_types:
                return False

        return True

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

        employee_match = re.search(r"\b(1-10|11-50|51-200|201-500|501-1000|1001-5000|5001\+)\s+Employees\b", page_text)
        return {"company": company, "role": title or "Untitled role", "location": location,
                "stipend": compensation, "description": description or "Description unavailable.",
                "employee_count": employee_match.group(1) if employee_match else "Not listed",
                "page_text": page_text, "link": link}

    @staticmethod
    def _location_url(country: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", country.casefold()).strip("-")
        return f"https://wellfound.com/location/{slug}" if slug else "https://wellfound.com/jobs"

    @staticmethod
    def _matches_filters(job: dict[str, str], filters: dict[str, str]) -> bool:
        keyword = filters.get("keyword", "").casefold().strip()
        if keyword and keyword not in job["role"].casefold():
            return False
        if filters.get("remote_only") == "true" and "remote" not in job["location"].casefold():
            return False
        if filters.get("company_size") and job["employee_count"] != filters["company_size"]:
            return False
        employment_type = filters.get("employment_type", "").casefold()
        if employment_type and employment_type not in job["page_text"].casefold():
            return False
        return True

    def scrape_jobs(self, maximum_jobs: int = 30, filters: dict[str, str] | None = None):
        """Import a bounded set of roles and their descriptions for dashboard review."""
        filters = filters or {}
        self.driver.get(self._location_url(filters.get("country", "")))
        candidate_limit = min(max(maximum_jobs * 5, 30), 100)
        listings = self._job_links()[:candidate_limit]
        candidates = [
            link for link, title, card_text in listings
            if self._matches_listing_filters(title, card_text, filters)
        ]
        self._report(
            f"Found {len(listings)} listings; {len(candidates)} may match. "
            "Loading descriptions for those candidates…"
        )
        self.data = []
        failures = []
        filtered_out = len(listings) - len(candidates)
        for index, link in enumerate(candidates, start=1):
            self._report(f"Checking candidate {index} of {len(candidates)} ({len(self.data)} matches)…")
            try:
                job = self._job_details(link)
                if self._matches_filters(job, filters):
                    job.pop("page_text", None)
                    self.data.append(job)
                    if len(self.data) >= maximum_jobs:
                        break
                else:
                    filtered_out += 1
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
        if not self.data:
            if failures:
                details = "; ".join(failures[:3])
                raise RuntimeError(
                    f"No jobs were imported. {len(failures)} listing page(s) failed to load"
                    f" and {filtered_out} were filtered out. First error(s): {details}"
                )
            raise RuntimeError(
                f"No jobs matched the selected filters ({filtered_out} listing(s) checked). "
                "Broaden the filters and try again."
            )
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
