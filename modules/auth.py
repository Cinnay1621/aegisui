# aegisui/auth.py
from functools import wraps
from flask import session, redirect, url_for, request, g
from modules.models import get_user_by_id

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        user = get_user_by_id(session["user_id"])
        if not user:
            return redirect(url_for("login"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper

def role_required(min_role):
    rank = {"guest": 1, "member": 2, "admin": 3}
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login", next=request.path))
            user = get_user_by_id(session["user_id"])
            if not user:
                return redirect(url_for("login"))
            if rank.get(user["role"], 0) < rank[min_role]:
                return "Unauthorized: insufficient permissions", 403
            g.user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator
