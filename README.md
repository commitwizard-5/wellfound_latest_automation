# Autoply

Autoply is a local-first AI job application workflow assistant. It collects Wellfound job listings, lets you review roles in a browser dashboard, and will draft tailored cover letters from your résumé using Gemini.

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies with `pip install -e .`.
3. Copy `.env.example` to `.env` and set your Gemini API key, Firefox profile path, and résumé path.
4. Run `python main.py` and open [http://127.0.0.1:8000](http://127.0.0.1:8000).

`templates/index.html` is a server-rendered Jinja template, not a standalone page. Its stylesheet can load when the file is opened directly, but template values and dashboard functionality require running `main.py`.

The job importer uses Wellfound's location results and pre-filters listing-card text when it contains enough information. It opens full job pages only for likely matches, then checks the filters again against job details. It does not use a separate Wellfound API.

The dashboard server binds only to your computer, but that does not mean all data stays on your device. When cover-letter generation is used, extracted résumé text and links, plus the job description, are sent to the Google Gemini API using your API key. Autoply cannot guarantee Google's processing or retention practices; review Google's current terms and use this feature only with the résumé owner's informed consent. Keep your API key in `.env` and never commit that file. Generated applications are recorded in `job_applications.xlsx`. For safety, Autoply only drafts and logs applications by default. Set `AUTO_SUBMIT=true` in `.env` only after reviewing your configuration and the selected roles.
