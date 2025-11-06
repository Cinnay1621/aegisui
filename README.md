# AegisUI

A Flask-based web UI for managing and executing Ansible-driven pipelines.

Overview
AegisUI provides a comprehensive, web-based interface to configure, run, and monitor
pipeline executions backed by Ansible. It features user authentication, project-based
RBAC, pipeline versioning, scheduling, live log streaming, and auditing. The UI is
built with Flask, SQLite for storage, and Bootstrap for styling.

Key Features
- User authentication with role-based access control (admin, manager, member, guest)
- Project-scoped memberships and permissions
- Create and manage pipelines (playbook, inventory, extra vars)
- Pipeline versioning and rollback
- Scheduling support per pipeline
- Live log streaming via Server-Sent Events (SSE)
- Admin tools for user and project management
- Audit logging for important actions

Getting Started
Prerequisites
- Python 3.9+ and pip
- SQLite (comes bundled with Python)

Installation
- python -m venv venv
- source venv/bin/activate
- pip install -r requirements.txt

Configuration
AegisUI reads configuration from environment variables (with sensible defaults):
- AEGIS_DB_PATH: Path to the SQLite database (default: ./aegis.db)
- AEGIS_LOG_DIR: Directory for log files (default: ./logs)
- AEGIS_READ_ONLY_DIRS: Comma-separated list of read-only dirs (default: /opt/aegis/readonly)
- AEGIS_ANSIBLE_BASE: Base directory for Ansible assets (default: /opt/aegis/ansible)
- AEGIS_HOST: Host to run the web UI (default: 127.0.0.1)
- AEGIS_PORT: Port to run the web UI (default: 5000)
- AEGIS_SECRET_KEY: Flask secret key (default: change-me)
- AEGIS_ADMIN_PASSWORD: Default admin user password (default: admin)
- AEGIS_ROW_LIMIT: Maximum number of pipelines shown on dashboards (default: 100)
- APP_NAME: Display name for the app (default: AegisUI)

Run the Application
- Ensure environment variables are set as needed (or rely on defaults)
- python -m aegisui.app  # or: python aegisui/app.py depending on your environment
- Visit http://127.0.0.1:5000/ (adjust host/port if you overridden AEGIS_HOST/AEGIS_PORT)

Notes
- The app uses SQLite by default for simplicity.
- Security-sensitive features (admin actions) are protected via login_required and role checks.
