"""Local FastAPI interface for the Autoply workflow.

The server intentionally binds to localhost only. Cover-letter generation
sends extracted résumé text, résumé links, and the job description to the
Google Gemini API. Review Google's current data-handling terms before use.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from autoapply.autoapply import JobScraper


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


class WorkflowState:
    """Small in-memory state store for one local Autoply session."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, str]] = []
        self.is_running = False
        self.message = "Configure Autoply, then import jobs from Wellfound."
        self.error = False
        self.filters = {"country": "India", "keyword": "", "remote_only": "false", "company_size": "", "employment_type": "", "limit": "30"}
        self.lock = threading.Lock()

    def set_message(self, message: str, *, error: bool = False) -> None:
        with self.lock:
            self.message = message
            self.error = error


state = WorkflowState()


def load_configuration() -> dict[str, str]:
    load_dotenv(ENV_FILE, override=True)
    return {
        "api_key": os.getenv("API_KEY", ""),
        "profile_path": os.getenv("PROFILE_PATH", ""),
        "resume_path": os.getenv("RESUME_PATH", ""),
        "auto_submit": os.getenv("AUTO_SUBMIT", "false").lower() == "true",
    }


def save_configuration(api_key: str, profile_path: str, resume_path: str) -> None:
    """Persist only local settings; a blank API-key field preserves the old key."""
    ENV_FILE.touch(exist_ok=True)
    if api_key.strip():
        set_key(str(ENV_FILE), "API_KEY", api_key.strip())
        os.environ["API_KEY"] = api_key.strip()
    if profile_path.strip():
        set_key(str(ENV_FILE), "PROFILE_PATH", profile_path.strip())
        os.environ["PROFILE_PATH"] = profile_path.strip()
    if resume_path.strip():
        set_key(str(ENV_FILE), "RESUME_PATH", resume_path.strip())
        os.environ["RESUME_PATH"] = resume_path.strip()
    if not os.getenv("AUTO_SUBMIT"):
        set_key(str(ENV_FILE), "AUTO_SUBMIT", "false")
        os.environ["AUTO_SUBMIT"] = "false"


def profile_configuration_errors(config: dict[str, str]) -> list[str]:
    errors = []
    profile_path = config["profile_path"]
    if not profile_path:
        errors.append("Add a Firefox profile path.")
    elif not Path(profile_path).exists():
        errors.append(f"Firefox profile path was not found: {profile_path}")
    return errors


def configuration_errors(config: dict[str, str]) -> list[str]:
    errors = []
    if not config["api_key"]:
        errors.append("Add a Gemini API key.")
    errors.extend(profile_configuration_errors(config))
    resume_path = config["resume_path"]
    if not resume_path:
        errors.append("Add a résumé path.")
    elif not Path(resume_path).exists():
        errors.append(f"Résumé path was not found: {resume_path}")
    return errors


def scrape_jobs_in_background(config: dict[str, str], filters: dict[str, str]) -> None:
    try:
        scraper = JobScraper(profile_path=config["profile_path"], progress_callback=state.set_message)
        try:
            scraper.scrape_jobs(maximum_jobs=int(filters["limit"]), filters=filters)
            jobs = [{**job, "id": str(index)} for index, job in enumerate(scraper.data)]
            with state.lock:
                state.jobs = jobs
                state.message = (
                    f"Imported {len(jobs)} job listings with descriptions."
                    f"{scraper.scrape_warning} Select roles to create drafts."
                )
                state.error = False
        finally:
            scraper.close()
    except Exception as exc:
        state.set_message(f"Could not import jobs: {exc}", error=True)
    finally:
        with state.lock:
            state.is_running = False


def create_app() -> FastAPI:
    app = FastAPI(title="Autoply", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

    @app.get("/")
    def dashboard(request: Request):
        config = load_configuration()
        with state.lock:
            jobs, message, error, is_running = list(state.jobs), state.message, state.error, state.is_running
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "config": config, "jobs": jobs, "message": message, "error": error,
            "is_running": is_running, "config_errors": configuration_errors(config), "filters": state.filters,
        })

    @app.post("/settings")
    def update_settings(api_key: str = Form(""), profile_path: str = Form(""), resume_path: str = Form("")):
        save_configuration(api_key, profile_path, resume_path)
        state.set_message("Local settings saved. Your API key is never shown in the dashboard.")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/jobs/import")
    def import_jobs(
        country: str = Form("India"), keyword: str = Form(""), remote_only: str = Form("false"),
        company_size: str = Form(""), employment_type: str = Form(""), limit: str = Form("30"),
    ):
        config = load_configuration()
        errors = profile_configuration_errors(config)
        if errors:
            state.set_message(" ".join(errors), error=True)
            return RedirectResponse(url="/", status_code=303)
        filters = {"country": country.strip(), "keyword": keyword.strip(), "remote_only": remote_only,
                   "company_size": company_size, "employment_type": employment_type, "limit": limit}
        with state.lock:
            if state.is_running:
                state.message = "An import is already in progress."
                return RedirectResponse(url="/", status_code=303)
            state.is_running, state.error = True, False
            state.jobs = []
            state.filters = filters
            state.message = "Opening Firefox and importing jobs. Keep this page open, then refresh shortly."
        threading.Thread(target=scrape_jobs_in_background, args=(config, filters), daemon=True).start()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/applications/draft")
    def draft_applications(job_ids: list[str] = Form(default=[])):
        selected = set(job_ids)
        with state.lock:
            selected_jobs = [job for job in state.jobs if job["id"] in selected]
        if not selected_jobs:
            state.set_message("Select at least one job before drafting applications.", error=True)
        else:
            state.set_message(
                f"{len(selected_jobs)} role(s) selected. Drafting from the dashboard is the next milestone; no application has been submitted."
            )
        return RedirectResponse(url="/", status_code=303)

    return app


def run() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
