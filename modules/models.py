# aegisui/models.py
import sqlite3
from datetime import datetime
from flask import g
from werkzeug.security import generate_password_hash, check_password_hash
from modules.config_builder import CONFIG

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(CONFIG["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    cur = db.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','guest','member'))
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_teams (
        user_id INTEGER,
        team_id INTEGER,
        PRIMARY KEY (user_id, team_id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (team_id) REFERENCES teams(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pipelines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        playbook_path TEXT NOT NULL,
        inventory_path TEXT NOT NULL,
        extra_vars_path TEXT,
        created_by INTEGER,
        created_at TEXT,
        project_id INTEGER,
        allowed_team_id INTEGER,
        FOREIGN KEY(created_by) REFERENCES users(id),
        FOREIGN KEY(allowed_team_id) REFERENCES teams(id),
        FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        name TEXT NOT NULL,
        playbook_path TEXT NOT NULL,
        inventory_path TEXT NOT NULL,
        extra_vars_path TEXT,
        created_by INTEGER,
        created_at TEXT,
        FOREIGN KEY(pipeline_id) REFERENCES pipelines(id),
        FOREIGN KEY(created_by) REFERENCES users(id)
    );
    """)

    # Scheduling, Jobs, Audits bleiben wie gehabt (ggf. angepasst, siehe unten)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT 1,
        next_run_at TEXT,
        interval_minutes INTEGER,
        FOREIGN KEY(pipeline_id) REFERENCES pipelines(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        stdout TEXT,
        stderr TEXT,
        depends_on_job_id INTEGER,
        FOREIGN KEY(pipeline_id) REFERENCES pipelines(id),
        FOREIGN KEY(depends_on_job_id) REFERENCES jobs(id)
    );
    """)
    db.commit()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        pipeline_id INTEGER,
        details TEXT,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(pipeline_id) REFERENCES pipelines(id)
    );
    """)
    db.commit()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_by INTEGER,
        created_at TEXT,
        FOREIGN KEY(created_by) REFERENCES users(id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_memberships (
        user_id INTEGER,
        project_id INTEGER,
        role TEXT NOT NULL,
        PRIMARY KEY (user_id, project_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    """)
    db.commit()

    cur.execute("SELECT id FROM users WHERE username = 'admin'")
    row = cur.fetchone()
    if not row:
        pw = CONFIG["ADMIN_PASSWORD"] or "admin"
        pw_hash = generate_password_hash(pw)
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", pw_hash, "admin"),
        )
        db.commit()

# Admin & projektbezogene Hilfsfunktionen

def get_all_users():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY id DESC")
    return cur.fetchall()

def delete_user(user_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM user_teams WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM project_memberships WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()

def update_user_role(user_id, role):
    db = get_db()
    cur = db.cursor()
    cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.commit()

def get_project_members(project_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT u.id as user_id, u.username, pm.role
        FROM users u
        JOIN project_memberships pm ON pm.user_id = u.id
        WHERE pm.project_id = ?
    """, (project_id,))
    return cur.fetchall()

def remove_user_from_project(user_id, project_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM project_memberships WHERE user_id = ? AND project_id = ?", (user_id, project_id))
    db.commit()

def query_user(username):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    return cur.fetchone()

def add_user(username, password, role):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), role),
    )
    db.commit()

def get_user_by_id(uid):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (uid,))
    return cur.fetchone()

def get_pipelines(limit=None):
    db = get_db()
    cur = db.cursor()
    if limit:
        cur.execute("SELECT * FROM pipelines ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur.execute("SELECT * FROM pipelines ORDER BY id DESC")
    return cur.fetchall()

def get_pipeline(pipeline_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
    return cur.fetchone()

def add_pipeline(name, playbook_path, inventory_path, extra_vars_path, created_by, project_id=None, allowed_team_id=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO pipelines (name, playbook_path, inventory_path, extra_vars_path, created_by, created_at, project_id, allowed_team_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, playbook_path, inventory_path, extra_vars_path, created_by, datetime.utcnow().isoformat(), project_id, allowed_team_id),
    )
    db.commit()
    pipeline_id = cur.lastrowid
    # Erstes Version-Snapshot erstellen
    cur.execute(
        """INSERT INTO pipeline_versions (pipeline_id, version, name, playbook_path, inventory_path, extra_vars_path, created_by, created_at)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
        (pipeline_id, name, playbook_path, inventory_path, extra_vars_path, created_by, datetime.utcnow().isoformat()),
    )
    db.commit()
    return pipeline_id

def add_job(pipeline_id, status="PENDING", started_at=None, ended_at=None, stdout="", stderr="", depends_on_job_id=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO jobs (pipeline_id, status, started_at, ended_at, stdout, stderr, depends_on_job_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pipeline_id, status, started_at, ended_at, stdout, stderr, depends_on_job_id),
    )
    db.commit()
    return cur.lastrowid

def update_job(job_id, **kwargs):
    db = get_db()
    cur = db.cursor()
    fields = []
    vals = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        vals.append(v)
    if not fields:
        return
    vals.append(job_id)
    sql = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"
    cur.execute(sql, tuple(vals))
    db.commit()

def get_job(job_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return cur.fetchone()

def get_jobs_for_pipeline(pipeline_id, limit=100):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM jobs WHERE pipeline_id = ? ORDER BY id DESC LIMIT ?", (pipeline_id, limit))
    return cur.fetchall()

def add_schedule(pipeline_id, interval_minutes, next_run_at=None, enabled=1):
    db = get_db()
    cur = db.cursor()
    if next_run_at is None:
        next_run_at = datetime.utcnow().isoformat()
    cur.execute(
        """INSERT INTO schedules (pipeline_id, enabled, next_run_at, interval_minutes)
           VALUES (?, ?, ?, ?)""",
        (pipeline_id, enabled, next_run_at, interval_minutes),
    )
    db.commit()

def get_schedule_by_pipeline(pipeline_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM schedules WHERE pipeline_id = ?", (pipeline_id,))
    return cur.fetchone()

def update_schedule_next_run(schedule_id, next_run_at=None, enabled=None, interval_minutes=None):
    db = get_db()
    cur = db.cursor()
    fields = []
    vals = []
    if next_run_at is not None:
        fields.append("next_run_at = ?"); vals.append(next_run_at)
    if enabled is not None:
        fields.append("enabled = ?"); vals.append(enabled)
    if interval_minutes is not None:
        fields.append("interval_minutes = ?"); vals.append(interval_minutes)
    if not fields:
        return
    vals.append(schedule_id)
    sql = f"UPDATE schedules SET {', '.join(fields)} WHERE id = ?"
    cur.execute(sql, tuple(vals))
    db.commit()

def get_schedules():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM schedules")
    return cur.fetchall()

def add_user_to_team(user_id, team_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT OR IGNORE INTO user_teams (user_id, team_id) VALUES (?, ?)", (user_id, team_id))
    db.commit()

def get_user_teams(user_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT t.id, t.name
        FROM teams t
        JOIN user_teams ut ON ut.team_id = t.id
        WHERE ut.user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    return [r["id"] for r in rows]

def has_team_access(user_id, pipeline):
    # Admin hat immer Zugriff
    user = get_user_by_id(user_id)
    if user and user["role"] == "admin":
        return True
    allowed = pipeline["allowed_team_id"]
    if allowed is None:
        # Offene Pipeline
        return True
    user_teams = get_user_teams(user_id)
    return enabled_in(user_teams, allowed)

def enabled_in(team_ids, allowed_id):
    return allowed_id in team_ids

def get_pipeline_versions(pipeline_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM pipeline_versions WHERE pipeline_id = ? ORDER BY version DESC", (pipeline_id,))
    return cur.fetchall()

def add_pipeline_version(pipeline_id, version, name, playbook_path, inventory_path, extra_vars_path, created_by):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO pipeline_versions (pipeline_id, version, name, playbook_path, inventory_path, extra_vars_path, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (pipeline_id, version, name, playbook_path, inventory_path, extra_vars_path, created_by, datetime.utcnow().isoformat()),
    )
    db.commit()

def rollback_pipeline_version(pipeline_id, version):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM pipeline_versions WHERE pipeline_id = ? AND version = ?", (pipeline_id, version))
    ver = cur.fetchone()
    if not ver:
        return False
    # Update current pipeline with version fields
    cur.execute("""
        UPDATE pipelines
        SET name = ?, playbook_path = ?, inventory_path = ?, extra_vars_path = ?, created_by = ?, created_at = ?
        WHERE id = ?
    """, (
        ver["name"], ver["playbook_path"], ver["inventory_path"], ver["extra_vars_path"],
        ver["created_by"], ver["created_at"], pipeline_id
    ))
    db.commit()
    return True

def log_audit(user_id, action, pipeline_id=None, details=""):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO audit_logs (user_id, action, pipeline_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, pipeline_id, details, datetime.utcnow().isoformat())
    )
    db.commit()

def get_audit_logs(limit=100):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
    return cur.fetchall()

def get_projects(limit=None):
    db = get_db()
    cur = db.cursor()
    if limit:
        cur.execute("SELECT * FROM projects ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur.execute("SELECT * FROM projects ORDER BY id DESC")
    return cur.fetchall()

def get_project(project_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    return cur.fetchone()

def add_project(name, created_by):
    db = get_db()
    cur = db.cursor()
    cur.execute("""INSERT INTO projects (name, created_by, created_at) VALUES (?, ?, ?)""",
                (name, created_by, datetime.utcnow().isoformat()))
    db.commit()
    return cur.lastrowid

def delete_project(project_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM project_memberships WHERE project_id = ?", (project_id,))
    cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()

def add_user_to_project(user_id, project_id, role):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM project_memberships WHERE user_id = ? AND project_id = ?", (user_id, project_id))
    cur.execute("INSERT INTO project_memberships (user_id, project_id, role) VALUES (?, ?, ?)", (user_id, project_id, role))
    db.commit()

def get_user_projects(user_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT p.id AS project_id, p.name, pm.role
        FROM projects p
        LEFT JOIN project_memberships pm ON pm.project_id = p.id AND pm.user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    result = []
    for r in rows:
        if r["project_id"] is None:
            continue
        result.append({"id": r["project_id"], "name": r["name"], "role": r["role"]})
    return result

def get_user_project_role(user_id, project_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT role FROM project_memberships WHERE user_id = ? AND project_id = ?", (user_id, project_id))
    row = cur.fetchone()
    return row["role"] if row else None
