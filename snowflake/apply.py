"""Apply the reviewed initial SQL files with an explicit --apply switch.

Run from the repository root: python snowflake/apply.py --help
No SQL or network request is issued when --apply / --status is absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
INITIAL = ("001_bootstrap.sql", "002_schema.sql", "003_access.sql", "007_password_login.sql")


def connect():
    import snowflake.connector

    connection_name = os.getenv("SNOWFLAKE_CONNECTION_NAME")
    options = {
        "role": "ACCOUNTADMIN",
        "login_timeout": 90,
        "network_timeout": 30,
        "session_parameters": {"TIMEZONE": "UTC", "STATEMENT_TIMEOUT_IN_SECONDS": 120},
    }
    if connection_name:
        options["connection_name"] = connection_name
    else:
        account, user = os.getenv("SNOWFLAKE_ACCOUNT"), os.getenv("SNOWFLAKE_USER")
        if not account or not user:
            raise ValueError("Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER, or SNOWFLAKE_CONNECTION_NAME.")
        options.update(account=account, user=user)
        authenticator = os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
        options["authenticator"] = authenticator
        key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
        if key_path:
            options["authenticator"] = "SNOWFLAKE_JWT"
            options["private_key_file"] = str(Path(key_path).expanduser())
            if os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
                options["private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
        elif authenticator.lower() == "snowflake" and os.getenv("SNOWFLAKE_PASSWORD"):
            options["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    return snowflake.connector.connect(**options)


def write_log(path, **entry):
    entry["at"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")


def print_status(connection):
    checks = (
        ("account", "SELECT CURRENT_ACCOUNT_NAME(), CURRENT_REGION(), CURRENT_ROLE()"),
        ("base_tables", "SELECT COUNT(*) FROM HACKATHON_DB.INFORMATION_SCHEMA.TABLES "
         "WHERE TABLE_SCHEMA = 'APP' AND TABLE_TYPE = 'BASE TABLE'"),
        ("search", "SHOW CORTEX SEARCH SERVICES IN SCHEMA HACKATHON_DB.APP"),
        ("agent", "SHOW AGENTS IN SCHEMA HACKATHON_DB.APP"),
        ("cross_region_policy", "SHOW PARAMETERS LIKE 'CORTEX_ENABLED_CROSS_REGION' IN ACCOUNT"),
    )
    with connection.cursor() as cursor:
        for label, sql in checks:
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
                if label == "account":
                    print(f"Account: {rows[0][0]}; region: {rows[0][1]}; role: {rows[0][2]}")
                elif label == "base_tables":
                    count = rows[0][0]
                    print(f"Base tables: {count}/11")
                elif label == "cross_region_policy":
                    columns = [item[0].lower() for item in cursor.description]
                    value_index = columns.index("value")
                    print(f"Cross-region policy: {rows[0][value_index] if rows else 'not returned'}")
                else:
                    columns = [item[0].lower() for item in cursor.description]
                    name_index = columns.index("name")
                    expected = "EVENT_SEARCH" if label == "search" else "EVENT_ASSISTANT"
                    found = any(str(row[name_index]).upper() == expected for row in rows)
                    print(f"{expected}: {'created (readiness not proven)' if found else 'not created'}")
            except Exception as error:
                print(f"{label}: unavailable (SQL state {getattr(error, 'sqlstate', 'unknown')})")
    print("Inference model: configured" if os.getenv("SNOWFLAKE_EXTRACT_MODEL") or os.getenv("CORTEX_MODEL")
          else "Inference model: not configured; no inference call was made")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Execute the selected SQL files, stopping at the first error")
    parser.add_argument("--include-search", action="store_true", help="Also create the billable Cortex Search service and Agent")
    parser.add_argument("--seed-tags", action="store_true", help="Merge the curated tag vocabulary as owner")
    parser.add_argument("--seed-clubs", action="store_true", help="Merge the reviewed real club/account mappings as owner")
    parser.add_argument("--status", action="store_true", help="Read account/table/service status; no inference calls")
    parser.add_argument("--log-path", type=Path,
                        default=Path(tempfile.gettempdir()) / "snowflask-setup.jsonl")
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE.parent / ".env")
    except ImportError:
        pass
    files = list(INITIAL)
    if args.seed_tags:
        files.append("seed_tags.sql")
    if args.seed_clubs:
        files.append("seed_clubs.sql")
    if args.include_search:
        files.append("004_search_agent.sql")
    if not args.apply and not args.status:
        print("Prepared files (no SQL executed):")
        for name in files:
            digest = hashlib.sha256((BASE / name).read_bytes()).hexdigest()
            print(f"  {name}  sha256={digest}")
        print("Add --apply to execute; --include-search starts billable Search serving/indexing.")
        return 0
    lock_path = Path(tempfile.gettempdir()) / "snowflask-setup.lock"
    lock_fd = None
    connection = None
    current_file = None
    try:
        if args.apply:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(lock_fd, str(os.getpid()).encode())
            args.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fd = os.open(args.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(log_fd)
        connection = connect()
        if args.apply:
            for name in files:
                current_file = name
                sql_path = BASE / name
                digest = hashlib.sha256(sql_path.read_bytes()).hexdigest()
                write_log(args.log_path, file=name, sha256=digest, outcome="started")
                print(f"Applying {name} ...", flush=True)
                with sql_path.open(encoding="utf-8") as stream:
                    for cursor in connection.execute_stream(stream, remove_comments=True):
                        write_log(args.log_path, file=name, sha256=digest, outcome="statement_ok",
                                  query_id=cursor.sfqid)
                        cursor.close()
                write_log(args.log_path, file=name, sha256=digest, outcome="completed")
                print(f"Completed {name}", flush=True)
            print(f"Deployment record: {args.log_path}")
            print("Snowflake DDL commits separately; completed statements remain after a later failure.")
        if args.status:
            print_status(connection)
        return 0
    except FileExistsError:
        print(f"Another setup runner may be active. Inspect {lock_path} before removing a stale lock.", file=sys.stderr)
        return 1
    except Exception as error:
        state = getattr(error, "sqlstate", None)
        code = getattr(error, "errno", None)
        if args.apply and lock_fd is not None:
            write_log(args.log_path, file=current_file, outcome="failed", sqlstate=state, error_code=code)
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print(f"Setup stopped at {current_file or 'connection'} "
                  f"(error type {type(error).__name__}, code {code}, SQL state {state}). "
                  "Inspect the Snowflake query history or local connector configuration.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
