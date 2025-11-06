# aegisui/config.py
import os

def load_config():
    cfg = {
        "DB_PATH": os.environ.get("AEGIS_DB_PATH", "./aegis.db"),
        "LOG_DIR": os.environ.get("AEGIS_LOG_DIR", "./logs"),
        "READ_ONLY_DIRS": os.environ.get("AEGIS_READ_ONLY_DIRS", "/opt/aegis/readonly").split(","),
        "ANSIBLE_BASE_DIR": os.environ.get("AEGIS_ANSIBLE_BASE", "/opt/aegis/ansible"),
        "HOST": os.environ.get("AEGIS_HOST", "127.0.0.1"),
        "PORT": int(os.environ.get("AEGIS_PORT", "5000")),
        "SECRET_KEY": os.environ.get("AEGIS_SECRET_KEY", "change-me"),
        "ADMIN_PASSWORD": os.environ.get("AEGIS_ADMIN_PASSWORD", "admin"),
        "ROW_LIMIT": int(os.environ.get("AEGIS_ROW_LIMIT", "100")),
        "APP_NAME": "AegisUI"
    }

    cfg["READ_ONLY_DIRS"] = [d.rstrip("/") for d in cfg["READ_ONLY_DIRS"] if d.strip()]
    cfg["ANSIBLE_BASE_DIR"] = cfg["ANSIBLE_BASE_DIR"].rstrip("/")

    return cfg

CONFIG = load_config()
