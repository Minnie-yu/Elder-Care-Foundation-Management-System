# Elder Care Management System

> A web-based management platform for coordinating donations, inventory, suppliers, staff, events, finance, and reporting in an elder-care organization.

**[Live demo on Render](https://eldercaremanagement.onrender.com/dashboard)**  
*The demonstration may require an account and availability depends on the Render service.*

## Project overview

This student team project brings several operational workflows into one Flask-based application. It provides a central dashboard, record management, reporting, and an optional AI-powered database assistant.

### Features

- **Dashboard:** Overview of donors, gifts, receivables, and operational records.
- **Donor & gift management:** Donor profiles, gift inventory, and gift distribution records.
- **Inventory & suppliers:** Inventory listing, item management, and supplier records.
- **Staff & events:** Staff information, events, and participant assignment.
- **Finance:** Invoices, payments, receivables, and reporting views.
- **Reports & export:** Operational and financial reporting, with CSV export endpoints.
- **AI assistant (optional):** Question-answering over database information using DeepSeek-compatible API integration.

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python, Flask |
| Frontend | Jinja2 templates, HTML, CSS, JavaScript |
| Database | TiDB (MySQL-compatible protocol) via PyMySQL |
| AI integration | DeepSeek API (OpenAI-compatible SDK) |
| Deployment | Render |

## Repository structure

```text
.
├── app.py                  # Flask application and routes
├── templates/              # Jinja2 views
├── static/                 # CSS, JavaScript and images
├── ca/isrgrootx1.pem       # Public CA certificate
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable placeholders (no secrets)
└── .gitignore              # Local secrets and generated files excluded
```

## Running locally

1. Have Python 3 installed and access to an appropriately configured TiDB/MySQL-compatible database.
2. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in **your own** database credentials and Flask secret. Leave `DEEPSEEK_API_KEY` empty if you do not need the AI assistant.

   ```bash
   cp .env.example .env
   ```
5. Run the application:

   ```bash
   python app.py
   ```

**Database setup note:** Database schema migrations, SQL initialization scripts, and sample datasets are not included in this repository snapshot. The application requires the corresponding existing schema; configuring environment variables alone is not enough to initialize a new database.

## My contributions

Within the team project, my work focused on frontend development and user experience, including the Gift, Inventory, and Report interfaces; search/filter interactions; and improvements to information display. I also supported project communication and multimedia materials.

## Screenshots

Add anonymized screenshots of the dashboard and key modules to a `docs/screenshots/` folder. Avoid including real personal or financial records in public screenshots.

## Project attribution

This is a collaborative student project. The source ZIP retained a link to the [original team repository](https://github.com/lxiaoran24-lab/elder_care). This portfolio repository documents my contributions and the project work. Please preserve appropriate credit to other contributors.

## Important security notes

This repository snapshot is for educational and portfolio purposes. It has **not** been audited or hardened for production deployment. Use test data; do not expose real personal information, passwords, or API keys. Review authentication/password storage, role authorization, and diagnostic routes before using it with live users. Never commit a real `.env` file.
