"""Master Orchestrator (Function App blueprint).

A timer-triggered blueprint that:
  1. Fetches a hardcoded list of child scripts from a GitHub repo at runtime.
  2. Runs each script in an isolated namespace via exec().
  3. Logs results and continues to the next script on failure.

Config (App Settings / environment variables):
  - GITHUB_REPO   : "owner/repo" (e.g. "Arnav082/azure-automation-script-function-app")
  - GITHUB_BRANCH : branch name (default "main")
  - GITHUB_PAT    : optional personal access token. Leave empty for public repos.
"""

import logging
import os
import traceback

import azure.functions as func
import requests

# Create the blueprint. It is registered in function_app.py.
bp = func.Blueprint()


def get_env(name: str, default: str = "") -> str:
    """Read a value from the environment (App Settings), with a default."""
    value = os.getenv(name, default)
    return value.strip() if value else default


# --- GitHub config -----------------------------------------------------------
GITHUB_REPO = get_env("GITHUB_REPO", "Arnav082/azure-automation-script-function-app")
GITHUB_BRANCH = get_env("GITHUB_BRANCH", "main")
# TODO: add a fine-grained, read-only PAT here later (via App Settings).
GITHUB_PAT = get_env("GITHUB_PAT", "")


def fetch_script(file_path: str) -> str:
    """Fetch a raw script from GitHub.

    Uses the GitHub contents API with the raw media type. When GITHUB_PAT is
    empty (public repo), no Authorization header is sent.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}?ref={GITHUB_BRANCH}"
    headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_PAT:
        headers["Authorization"] = f"token {GITHUB_PAT}"

    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to load {file_path}: HTTP {response.status_code}"
        )
    return response.text


def run_child_script(file_path: str) -> None:
    """Fetch and execute a single child script in an isolated namespace."""
    logging.info("Running child script: %s", file_path)
    source = fetch_script(file_path)
    # Isolated namespace so a failing script can't corrupt the next one.
    exec(source, {"__name__": "__main__"})


# List of child scripts to run, in order. Paths are relative to the repo root.
# Add new runbooks here as you create them.
CHILD_SCRIPTS = [
    "runbooks/monitor.py",
    # "runbooks/another_script.py",
    # "runbooks/yet_another_script.py",
]


# Timer trigger: runs every 30 minutes.
# NCRONTAB: second minute hour day month day-of-week.
@bp.schedule(schedule="0 */30 * * * *", arg_name="myTimer", run_on_startup=False)
def master_orchestrator(myTimer: func.TimerRequest) -> None:
    logging.info("Starting Master Orchestrator...")

    for script in CHILD_SCRIPTS:
        try:
            run_child_script(script)
        except Exception as exc:  # noqa: BLE001 - keep going on per-script errors
            logging.error("ERROR running %s: %s", script, exc)
            logging.error(traceback.format_exc())

    logging.info("Master Orchestrator completed.")