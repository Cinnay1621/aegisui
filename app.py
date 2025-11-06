# aegisui/app.py
from flask import Flask, render_template, request, redirect, url_for, session, g, Response, jsonify, current_app
import threading
import queue
import subprocess
from datetime import datetime
import time
import os
from pathlib import Path
from werkzeug.security import check_password_hash

from modules.config_builder import CONFIG
from modules.models import (
    init_db,
    get_pipeline, get_pipelines,
    get_job, get_jobs_for_pipeline, add_job, update_job, add_pipeline,
    get_user_by_id, get_projects, get_user_projects, get_user_project_role,
    log_audit, add_pipeline_version, get_pipeline_versions, rollback_pipeline_version,
    add_user_to_team, get_user_teams, has_team_access,
    add_project, add_user_to_project, get_project, delete_project,
    get_all_users, delete_user, update_user_role, get_project_members, remove_user_from_project,
    query_user, add_user
)

from modules.auth import login_required, role_required

# Hintergrund-Worker-Queue
JOB_QUEUE = queue.Queue()
WORKER_THREAD = None

# Live-Log-Streaming: Mapping von Job-ID -> Queue mit Logzeilen
LOG_STREAMS = {}

# ----------------------------
# Hilfsfunktionen
# ----------------------------
def is_path_within_dirs(path, bases):
    try:
        path = Path(path).resolve(strict=True)
    except FileNotFoundError:
        return False
    for base in bases:
        try:
            basep = Path(base).resolve(strict=True)
        except FileNotFoundError:
            continue
        if basep in path.parents or path == basep:
            return True
    return False

def check_ansible_paths(playbook_path, inventory_path, extra_vars_path):
    ro_dirs = [d.rstrip("/") for d in CONFIG["READ_ONLY_DIRS"]]
    if not (os.path.exists(playbook_path) and os.path.exists(inventory_path)):
        return False, "Playbook oder Inventory existiert nicht."
    if not is_path_within_dirs(playbook_path, ro_dirs):
        return False, "Playbook-Pfad liegt nicht in den read-only Verzeichnissen."
    if not is_path_within_dirs(inventory_path, ro_dirs):
        return False, "Inventory-Pfad liegt nicht in den read-only Verzeichnissen."
    if extra_vars_path:
        if not os.path.exists(extra_vars_path):
            return False, "Extra Vars-Datei existiert nicht."
        if not is_path_within_dirs(extra_vars_path, ro_dirs):
            return False, "Extra Vars-Pfad liegt nicht in den read-only Verzeichnissen."
    return True, "OK"

def _log_path_for_job(job_id):
    log_dir = Path(CONFIG["LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"job_{job_id}.log"

def ansible_worker():
    while True:
        job_id = JOB_QUEUE.get()
        try:
            # Sicherstellen, dass DB-Zugriffe im Anwendungs-Kontext erfolgen
            with app.app_context():
                job = get_job(job_id)
                if not job:
                    continue
                pipeline = get_pipeline(job["pipeline_id"])
                if not pipeline:
                    update_job(job_id, status="FAILED", ended_at=datetime.utcnow().isoformat(), stdout="", stderr="Pipeline not found.")
                    continue

                # Abhängigkeiten beachten
                if job.get("depends_on_job_id"):
                    parent = get_job(job["depends_on_job_id"])
                    if not parent or parent["status"] != "SUCCEEDED":
                        update_job(job_id, status="BLOCKED", started_at=None)
                        time.sleep(5)
                        JOB_QUEUE.put(job_id)
                        continue

                playbook_path = pipeline["playbook_path"]
                inventory_path = pipeline["inventory_path"]
                extra_vars_path = pipeline["extra_vars_path"]

                ok, msg = check_ansible_paths(playbook_path, inventory_path, extra_vars_path)
                if not ok:
                    update_job(job_id, status="FAILED", ended_at=datetime.utcnow().isoformat(), stdout="", stderr=msg)
                    continue

                cmd = ["ansible-playbook", playbook_path, "-i", inventory_path]
                if extra_vars_path:
                    cmd += ["--extra-vars", f"@{extra_vars_path}"]

                env = {
                    "LC_ALL": "C",
                    "LANG": "C",
                    "PATH": os.environ.get("PATH", "")
                }

                update_job(job_id, status="RUNNING", started_at=datetime.utcnow().isoformat())

                log_path = _log_path_for_job(job_id)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as log_file:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        env=env
                    )
                    stdout_chunks = []
                    try:
                        for line in proc.stdout:
                            stdout_chunks.append(line)
                            if job_id in LOG_STREAMS:
                                LOG_STREAMS[job_id].put(line)
                            log_file.write(line)
                            log_file.flush()
                        proc.wait()
                    except Exception as e:
                        log_file.write(str(e))
                    stdout_all = "".join(stdout_chunks)
                    ended_at = datetime.utcnow().isoformat()
                    if proc.returncode == 0:
                        update_job(job_id, status="SUCCEEDED", ended_at=ended_at, stdout=stdout_all, stderr="")
                    else:
                        update_job(job_id, status="FAILED", ended_at=ended_at, stdout=stdout_all, stderr=f"Exit code {proc.returncode}")
        except Exception as e:
            # Sicherstellen, dass DB-Calls auch im Fehlerfall über den Application Context laufen
            with app.app_context():
                update_job(job_id, status="FAILED", ended_at=datetime.utcnow().isoformat(),
                           stdout="", stderr=str(e))
        finally:
            JOB_QUEUE.task_done()

def start_worker():
    global WORKER_THREAD
    if WORKER_THREAD is None:
        WORKER_THREAD = threading.Thread(target=ansible_worker, daemon=True)
        WORKER_THREAD.start()

# Scheduler Thread
def scheduler_loop():
    from datetime import datetime, timedelta
    while True:
        try:
            now = datetime.utcnow().isoformat()
            schedules = []
            for s in get_schedules():
                if s["enabled"] and s["next_run_at"]:
                    schedules.append(s)
            for s in schedules:
                next_run = s["next_run_at"]
                if next_run <= datetime.utcnow().isoformat():
                    # Trigger
                    enqueue_job(s["pipeline_id"])
                    # Nächsten Lauf planen
                    interval = s["interval_minutes"] or 0
                    if interval > 0:
                        next_run_at = (datetime.utcnow() + timedelta(minutes=interval)).isoformat()
                        update_schedule_next_run(s["id"], next_run_at=next_run_at)
                        log_audit(None, "SCHEDULED_RUN", s["pipeline_id"], f"Next run at {next_run_at}")
                    else:
                        update_schedule_next_run(s["id"], enabled=0)
        except Exception as e:
            # ruhig weitermachen, Logging hier optional
            pass
        time.sleep(60)

# Job-Queue
def enqueue_job(pipeline_id, depends_on_job_id=None):
    from modules.models import add_job
    user_id = session.get("user_id")
    j_id = add_job(pipeline_id, status="PENDING", started_at=None, ended_at=None, depends_on_job_id=depends_on_job_id)
    # Audit
    if user_id:
        log_audit(user_id, "ENQUEUE_JOB", pipeline_id, f"job_id={j_id}, depends_on={depends_on_job_id}")
    else:
        log_audit(None, "ENQUEUE_JOB", pipeline_id, f"job_id={j_id}, depends_on={depends_on_job_id}")
    JOB_QUEUE.put(j_id)
    # Prepare log stream
    LOG_STREAMS.setdefault(j_id, queue.Queue())
    return j_id

# Bootstrap-Funktion (kein decorator)
def bootstrap():
    # Init DB im Application-Kontext
    from modules.models import init_db as _init_db
    with app.app_context():
        _init_db()

def ensure_directories():
    log_dir = Path(CONFIG["LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)

# Flask-App
app = Flask(__name__)
app.secret_key = CONFIG["SECRET_KEY"]

# ----------------------------
# Routen
# ----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = None
        try:
            from modules.models import query_user
            user = query_user(username)
        except Exception:
            user = None
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        else:
            error = "Ungültige Anmeldedaten"
    return render_template("login.html", error=error, APP_NAME=CONFIG["APP_NAME"]);

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Admin: Benutzerverwaltung (Admin-only)
@app.route("/admin/users", methods=["GET","POST"])
@login_required
@role_required("admin")
def admin_users():
    error = None
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "reader")
            if not username or not password:
                error = "Username und Passwort sind erforderlich."
            else:
                existing = query_user(username)
                if existing:
                    error = "Benutzer existiert bereits."
                else:
                    add_user(username, password, role)
                    log_audit(None, "ADMIN_ADD_USER", None, f"username={username}, role={role}")
        elif action == "add_project":
            name = request.form.get("project_name", "").strip()
            if name:
                pid = add_project(name, g.user["id"])
                log_audit(g.user["id"], "CREATE_PROJECT", pid, f"name={name}")
        elif action == "assign_to_project":
            user_id = request.form.get("user_id")
            project_id = request.form.get("project_id")
            role = request.form.get("role", "member")
            if user_id and project_id:
                add_user_to_project(int(user_id), int(project_id), role)
        elif action == "update":
            user_id = request.form.get("user_id")
            new_role = request.form.get("role", "reader")
            if user_id:
                update_user_role(int(user_id), new_role)
        elif action == "delete":
            user_id = request.form.get("user_id")
            if user_id:
                delete_user(int(user_id))
        return redirect(url_for("admin_users"))
    users = get_all_users()
    projects = get_projects()
    return render_template("admin_users.html", users=users, projects=projects, error=error, APP_NAME=CONFIG["APP_NAME"])
    
# Project Settings: Mitgliederverwaltung (Admin oder Project-Manager)
@app.route("/project/<int:project_id>/settings", methods=["GET","POST"])
@login_required
def project_settings(project_id):
    from modules.models import get_project, get_all_users, get_project_members
    from modules.models import add_user_to_project, remove_user_from_project, get_user_project_role
    project = get_project(project_id)
    if not project:
        return "Project not found.", 404
    # Zugriff prüfen: Admin oder Manager des Projekts
    if g.user["role"] != "admin":
        pid = project_id
        role = get_user_project_role(g.user["id"], pid) if pid else None
        if not pid or role != "manager":
            return "Unauthorized: insufficient permissions", 403

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add":
            user_id = int(request.form.get("user_id"))
            role = request.form.get("role", "member")
            add_user_to_project(user_id, project_id, role)
        elif action == "update":
            user_id = int(request.form.get("user_id"))
            role = request.form.get("role", "member")
            add_user_to_project(user_id, project_id, role)
        elif action == "remove":
            user_id = int(request.form.get("user_id"))
            remove_user_from_project(user_id, project_id)
        return redirect(url_for("project_settings", project_id=project_id))

    users = get_all_users()
    members = get_project_members(project_id)
    return render_template("project_settings.html", project=project, members=members, users=users, APP_NAME=CONFIG["APP_NAME"])

@app.route("/")
@login_required
def dashboard():
    from modules.models import get_pipelines, get_jobs_for_pipeline, get_projects
    # Adminen direkt auf Admin-Seite weiterleiten
    user_id = session.get("user_id")
    user = get_user_by_id(user_id)
    if user and user["role"] == "admin":
        return redirect(url_for("admin_users"))

    # Alle Pipelines in einem Durchlauf holen
    pipelines_all = get_pipelines(CONFIG["ROW_LIMIT"])

    # Projektdaten des Nutzers bzw. Admin
    user_projects = []
    current_project_id = None
    current_project_name = None
    projects = []

    if user and user["role"] != "admin":
        user_projects = get_user_projects(user_id)
        if not user_projects:
            return render_template("no_project.html")
        projects = [{"id": p["id"], "name": p["name"]} for p in user_projects]
        requested_pid = request.args.get("project_id")
        if requested_pid is None:
            # Redirect to first project
            first_id = projects[0]["id"]
            return redirect(url_for("dashboard", project_id=first_id))
        try:
            current_project_id = int(request.args.get("project_id"))
        except (TypeError, ValueError):
            current_project_id = None
        # Bestimme aktuellen Projekt-Namen
        for pr in user_projects:
            if pr["id"] == current_project_id:
                current_project_name = pr["name"]
                break
        if current_project_id is None:
            current_project_id = projects[0]["id"]
            current_project_name = projects[0]["name"]
    else:
        # Adminenpfad: alle Projekte laden (nicht erwartet, Redirect oben)
        projects = [{"id": p["id"], "name": p["name"]} for p in get_projects()]
        current_project_id = None
        current_project_name = None

    # Filtere Pipelines auf das aktuelle Projekt, falls gesetzt
    if current_project_id is not None:
        pipelines_all = [p for p in pipelines_all if p.get("project_id") == current_project_id]

    # Pipeline-Liste mit Berechtigungen annotieren
    pipelines = []
    for p in pipelines_all:
        pip = dict(p)
        pid = pip.get("project_id")
        role = None
        if user and user["role"] != "admin" and pid is not None:
            role = get_user_project_role(user_id, pid)
        pip["project_id"] = pid
        pip["project_role"] = role
        if user and user["role"] != "admin":
            if pid is None:
                can_run = False; can_schedule = False; can_configure = False
            else:
                can_run = role in ("manager", "member")
                can_schedule = can_run
                can_configure = (role == "manager")
        else:
            can_run = can_schedule = can_configure = True

        pip["can_run"] = can_run
        pip["can_schedule"] = can_schedule
        pip["can_configure"] = can_configure
        pipelines.append(pip)

    can_edit_current_project = False
    if user and user["role"] != "admin" and current_project_id is not None:
        role_for_current = get_user_project_role(user_id, current_project_id)
        can_edit_current_project = (role_for_current == "manager")

    return render_template("dashboard.html",
                           pipelines=pipelines,
                           recent_jobs=[],  # später mit Logik füllen
                           user=user,
                           APP_NAME=CONFIG["APP_NAME"],
                           projects=projects,
                           current_project_name=current_project_name,
                           current_project_id=current_project_id,
                           can_edit_current_project=can_edit_current_project)

@app.route("/admin")
@login_required
@role_required("admin")
def admin_index():
    return redirect(url_for("admin_users"))

@app.route("/pipeline/new", methods=["GET", "POST"])
@login_required
def create_pipeline():
    if request.method == "POST":
        if getattr(g, "user", None) and g.user["role"] != "admin":
            return "Unauthorized: only admins can create pipelines.", 403
        name = request.form.get("name", "").strip()
        playbook_path = request.form.get("playbook_path", "").strip()
        inventory_path = request.form.get("inventory_path", "").strip()
        extra_vars_path = request.form.get("extra_vars_path", "").strip() or None
        project_id = request.form.get("project_id", "")
        try:
            project_id = int(project_id) if project_id else None
        except ValueError:
            project_id = None
        allowed_team_id = request.form.get("allowed_team_id", "") or None

        if not (name and playbook_path and inventory_path):
            return "Name, Playbook und Inventory required.", 400

        # RBAC: nur Admin oder Projekt-Manger/Mitglied
        if g.user["role"] != "admin":
            if project_id is None:
                return "Projekt auswählen.", 400
            role = get_user_project_role(g.user["id"], project_id)
            if role not in ("manager", "member"):
                return "Unauthorized: project access required.", 403

        created_by = g.user["id"]
        from modules.models import add_pipeline
        pipeline_id = add_pipeline(name, playbook_path, inventory_path, extra_vars_path, created_by, project_id, allowed_team_id)
        log_audit(created_by, "CREATE_PIPELINE", pipeline_id, f"project_id={project_id}")
        return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))
    else:
        return redirect(url_for("dashboard"))

@app.route("/pipeline/<int:pipeline_id>", methods=["GET"])
@login_required
def view_pipeline(pipeline_id):
    from modules.models import get_pipeline, get_jobs_for_pipeline, get_pipeline_versions, get_schedule_by_pipeline
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return "Pipeline not found.", 404
    jobs = get_jobs_for_pipeline(pipeline_id, limit=50)
    versions = get_pipeline_versions(pipeline_id)
    sched = get_schedule_by_pipeline(pipeline_id)
    user = get_user_by_id(session.get("user_id"))
    return render_template("pipeline_view.html",
                           pipeline=pipeline, jobs=jobs, versions=versions, schedule=sched, user=user,
                           APP_NAME=CONFIG["APP_NAME"])

@app.route("/pipeline/<int:pipeline_id>/jobs_json")
@login_required
def pipeline_jobs_json(pipeline_id):
    # Pipeline prüfen
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return jsonify({"error": "Pipeline not found"}), 404

    # Einfache RBAC-Prüfung
    user = get_user_by_id(session.get("user_id"))
    if user and user["role"] != "admin":
        user_teams = get_user_teams(user["id"])
        allowed = pipeline["allowed_team_id"]
        if not (allowed is None or allowed in user_teams):
            return jsonify({"error": "Unauthorized"}), 403

    jobs = get_jobs_for_pipeline(pipeline_id, limit=50)
    data = [
        {"id": j["id"], "status": j["status"], "started_at": j["started_at"], "ended_at": j["ended_at"]}
        for j in jobs
    ]
    return jsonify(data)

@app.route("/pipeline/<int:pipeline_id>/start", methods=["POST"])
@login_required
def start_job(pipeline_id):
    if g.user["role"] != "admin":
        from modules.models import get_pipeline
        pipeline = get_pipeline(pipeline_id)
        if not pipeline:
            return "Pipeline not found.", 404
        pid = pipeline["project_id"]
        role = get_user_project_role(g.user["id"], pid) if pid else None
        if not pid or role not in ("manager", "member"):
            return "Unauthorized: insufficient permissions", 403
            
    from modules.models import get_pipeline
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return "Pipeline not found.", 404
    enqueue_job(pipeline_id)
    log_audit(g.user["id"], "START_PIPELINE", pipeline_id)
    return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))

@app.route("/run/<int:pipeline_id>", methods=["GET"])
@login_required
def run_pipeline(pipeline_id):
    pipeline = None
    from modules.models import get_pipeline
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return "Pipeline not found.", 404
    # Fein-granulare RBAC-Pruefung
    if g.user["role"] != "admin":
        pid = pipeline["project_id"]
        role = get_user_project_role(g.user["id"], pid) if pid else None
        if not pid or role not in ("manager", "member"):
            return "Unauthorized: insufficient permissions", 403
    enqueue_job(pipeline_id)
    log_audit(g.user["id"], "ENQUEUE_PIPELINE", pipeline_id)
    return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))

# Scheduling Endpoints
@app.route("/pipeline/<int:pipeline_id>/schedule", methods=["GET", "POST"])
@login_required
def pipeline_schedule(pipeline_id):
    from modules.models import get_pipeline
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return "Pipeline not found.", 404
    if request.method == "POST":
        if g.user["role"] != "admin":
            return "Unauthorized: only admins can schedule.", 403
        interval = request.form.get("interval_minutes", "").strip()
        try:
            interval = int(interval)
        except Exception:
            interval = 0
        if interval <= 0:
            return "Interval must be > 0", 400
        sched = get_schedule_by_pipeline(pipeline_id)
        if sched:
            update_schedule_next_run(sched["id"], next_run_at=datetime.utcnow().isoformat(), interval_minutes=interval)
        else:
            add_schedule(pipeline_id, interval_minutes=interval, next_run_at=datetime.utcnow().isoformat())
        log_audit(g.user["id"], "SCHEDULE_SET", pipeline_id, f"interval={interval}")
        return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))
    else:
        return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))

# Versioning Endpoints
@app.route("/pipeline/<int:pipeline_id>/version/new", methods=["POST"])
@login_required
def pipeline_new_version(pipeline_id):
    from modules.models import get_pipeline, add_pipeline_version
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        return "Pipeline not found.", 404
    if g.user["role"] != "admin":
        pid = pipeline["project_id"]
        role = get_user_project_role(g.user["id"], pid) if pid else None
        if role != "manager":
            return "Unauthorized: only project managers can version.", 403
    versions = __import__('modules.models').models.get_pipeline_versions(pipeline_id)
    max_ver = max([v["version"] for v in versions], default=0) if versions else 0
    new_ver = max_ver + 1
    add_pipeline_version(
        pipeline_id=pipeline_id,
        version=new_ver,
        name=pipeline["name"],
        playbook_path=pipeline["playbook_path"],
        inventory_path=pipeline["inventory_path"],
        extra_vars_path=pipeline["extra_vars_path"],
        created_by=g.user["id"]
    )
    log_audit(g.user["id"], "VERSION_CREATED", pipeline_id, f"version={new_ver}")
    return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))

@app.route("/pipeline/<int:pipeline_id>/version/<int:version>/rollback", methods=["POST"])
@login_required
def pipeline_version_rollback(pipeline_id, version):
    if g.user["role"] != "admin":
        return "Unauthorized: only admins can rollback.", 403
    ok = rollback_pipeline_version(pipeline_id, version)
    if not ok:
        return "Version not found.", 404
    log_audit(g.user["id"], "VERSION_ROLLBACK", pipeline_id, f"to_version={version}")
    return redirect(url_for("view_pipeline", pipeline_id=pipeline_id))

@app.route("/projects", methods=["GET"])
@login_required
def projects_list():
    if g.user["role"] != "admin":
        return "Unauthorized", 403
    from modules.models import get_projects
    projects = get_projects()
    return render_template("projects.html", projects=projects, APP_NAME=CONFIG["APP_NAME"])

@app.route("/project/new", methods=["POST"])
@login_required
def project_create():
    # Admin-only
    if g.user["role"] != "admin":
        return "Unauthorized", 403
    name = request.form.get("name", "").strip()
    if not name:
        return "Name required", 400
    pid = add_project(name, g.user["id"])
    log_audit(g.user["id"], "CREATE_PROJECT", pid, f"name={name}")
    return redirect(url_for("projects_list"))

@app.route("/project/<int:project_id>/delete", methods=["POST"])
@login_required
def project_delete(project_id):
    if g.user["role"] != "admin":
        return "Unauthorized", 403
    delete_project(project_id)
    log_audit(g.user["id"], "DELETE_PROJECT", project_id)
    return redirect(url_for("projects_list"))

# Logs Streaming Endpoint (SSE)
@app.route("/logs/<int:job_id>/stream")
@login_required
def stream_log(job_id):
    from modules.models import get_job
    def gen():
        log_path = _log_path_for_job(job_id)
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        yield f"data: {line.rstrip()}\\n\\n"
            except Exception:
                pass
        q = LOG_STREAMS.get(job_id, queue.Queue())
        while True:
            try:
                line = q.get(timeout=2)
                if line == "__END__":
                    break
                yield f"data: {line.rstrip()}\\n\\n"
            except queue.Empty:
                # Hier Kontext wiederherstellen, damit DB-Zugriffe funktionieren
                with current_app.app_context():
                    current = get_job(job_id)
                if current and current["status"] in ("SUCCEEDED","FAILED","BLOCKED"):
                    break
        yield "data: [END]\\n\\n"
    return Response(gen(), mimetype="text/event-stream")

# ----------------------------
# Startup
# ----------------------------
if __name__ == "__main__":
    ensure_directories()
    bootstrap()
    start_worker()
    # Scheduler starten
    sched_thread = threading.Thread(target=scheduler_loop, daemon=True)
    sched_thread.start()
    app.run(host=CONFIG["HOST"], port=CONFIG["PORT"], debug=False)
