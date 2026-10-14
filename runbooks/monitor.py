"""Azure Reservation Utilization Monitor.

Production-ready automation that:
  1. Authenticates to Azure using a service principal (ClientSecretCredential).
  2. Discovers all reservations visible to the service principal.
  3. Retrieves per-reservation utilization (last-day and 7-day) from the
     Azure Consumption "Reservation Summaries" API.
  4. Flags reservations below a configurable utilization threshold.
  5. Prints a structured report and optionally sends a Slack alert.

Usage:
    python monitor.py
"""

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.consumption import ConsumptionManagementClient
from azure.mgmt.reservations import ReservationsMgmtClient
from dotenv import load_dotenv

# Load environment variables from the .env file (if present).
load_dotenv()

# In Azure Automation, config lives in Automation Variables (accessed via
# `automationassets`). Locally, it lives in environment variables / .env.
# `get_variable` tries Automation first, then falls back to the environment.
try:
    from automationassets import get_automation_variable
except ImportError:  # running locally, outside Azure Automation
    get_automation_variable = None


def get_variable(name: str, default: str = "") -> str:
    """Return a config value from Azure Automation Variables, falling back to
    environment variables (and finally `default`).

    `get_automation_variable` accepts only a single argument (the variable
    name), so we wrap it to provide a default-value fallback ourselves.
    """
    if get_automation_variable is not None:
        try:
            value = get_automation_variable(name)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
        except Exception:
            pass
    return os.getenv(name, default)


# --- Configuration -----------------------------------------------------------
# Utilization threshold (percent). A reservation is marked UNDERUTILIZED when
# either its last-day OR its 7-day utilization falls below this value.
UTILIZATION_THRESHOLD = float(get_variable("UTILIZATION_THRESHOLD", "80"))

# Number of days used to compute the 7-day utilization average.
UTILIZATION_DAYS = int(get_variable("UTILIZATION_DAYS", "7"))

# Slack incoming webhook URL. Empty = print the report to stdout only.
SLACK_WEBHOOK_URL = get_variable("SLACK_WEBHOOK_URL", "")

# When true, only send a Slack alert if at least one reservation is underutilized.
ALERT_ONLY_UNDERUTILIZED = get_variable("ALERT_ONLY_UNDERUTILIZED", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Azure service principal credentials (read from Automation Variables / env).
TENANT_ID = get_variable("AZURE_TENANT_ID", "")
CLIENT_ID = get_variable("AZURE_CLIENT_ID", "")
CLIENT_SECRET = get_variable("AZURE_CLIENT_SECRET", "")

# Subscription ID used to construct the Consumption client. The reservation
# summaries endpoint is tenant-scoped, but the SDK still requires this value.
SUBSCRIPTION_ID = get_variable("AZURE_SUBSCRIPTION_ID", "") or get_variable("SUBSCRIPTION_ID", "")

# --- Logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("reservation-monitor")

# Silence the Azure SDK's verbose HTTP request/response logging so our own
# log messages stay readable. Set these to DEBUG only when troubleshooting.
for _noisy in ("azure.identity", "azure.core", "azure.mgmt.reservations",
               "azure.mgmt.consumption", "msal", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# --- Data models -------------------------------------------------------------
@dataclass
class ReservationRecord:
    """A reservation with metadata, utilization, and evaluated health status."""

    name: str
    reservation_id: str
    status: str
    expiry_date: str
    scope: str
    reservation_type: str
    product: str
    region: str
    renewal: str
    quantity: int
    last_day: Optional[float] = None
    seven_day: Optional[float] = None
    health: str = "UNKNOWN"
    error: Optional[str] = None


# --- Authentication ----------------------------------------------------------
def get_credential() -> ClientSecretCredential:
    """Build a ClientSecretCredential from environment variables.

    Raises ValueError if any required credential is missing.
    """
    missing = [
        name
        for name, value in (
            ("AZURE_TENANT_ID", TENANT_ID),
            ("AZURE_CLIENT_ID", CLIENT_ID),
            ("AZURE_CLIENT_SECRET", CLIENT_SECRET),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )
    return ClientSecretCredential(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


# --- Reservation discovery ---------------------------------------------------
def get_reservation_ids(reservation) -> tuple[str, str]:
    """Extract (order_id, reservation_id) from a reservation's resource id.

    The id looks like:
        /providers/Microsoft.Capacity/reservationOrders/{orderId}/reservations/{reservationId}
    """
    parts = [p for p in reservation.id.split("/") if p]
    order_id = parts[parts.index("reservationOrders") + 1]
    reservation_id = parts[parts.index("reservations") + 1]
    return order_id, reservation_id


def discover_reservations(client: ReservationsMgmtClient) -> list:
    """Return all reservations visible to the credential.

    Uses the Reservations API `list_all` operation, which is tenant-scoped and
    returns reservations across all subscriptions the principal can access.
    Pagination is handled automatically by the SDK's ItemPaged iterator.
    """
    return list(client.reservation.list_all())


def _safe_str(value) -> str:
    """Return a value as a trimmed string, or '-' when empty/None."""
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def build_record(reservation) -> ReservationRecord:
    """Map a Reservations API `ReservationResponse` to a ReservationRecord."""
    props = reservation.properties
    sku = reservation.sku

    # Friendly display name, falling back to the reservation GUID.
    name = (props.display_name if props and props.display_name else None) or \
        reservation.name or "Unnamed reservation"

    # Renewal status: the Reservations API exposes a boolean `renew` flag.
    renew = getattr(props, "renew", None) if props else None
    renewal = "On" if renew is True else ("Off" if renew is False else "-")

    # Expiration date: prefer the date field, fall back to the datetime field.
    expiry = (props.expiry_date if props and props.expiry_date else None) or \
        (props.expiry_date_time if props and props.expiry_date_time else None)

    return ReservationRecord(
        name=name,
        reservation_id=reservation.name or "-",
        status=_safe_str(props.provisioning_state if props else None),
        expiry_date=_safe_str(expiry),
        scope=_safe_str(props.applied_scope_type if props else None),
        reservation_type=_safe_str(props.reserved_resource_type if props else None),
        product=_safe_str(sku.name if sku else None),
        region=_safe_str(reservation.location),
        renewal=renewal,
        quantity=int(props.quantity) if props and props.quantity is not None else 0,
    )


# --- Utilization retrieval ---------------------------------------------------
def _summary_avg(summary) -> Optional[float]:
    """Return the avg utilization percentage from a ReservationSummary object.

    In azure-mgmt-consumption >= 11.0.0 the utilization fields live under
    `properties`. Older versions exposed them directly on the model, so we
    support both shapes.
    """
    props = getattr(summary, "properties", None)
    if props is not None:
        avg = getattr(props, "avg_utilization_percentage", None)
    else:
        avg = getattr(summary, "avg_utilization_percentage", None)
    return float(avg) if avg is not None else None


def get_utilization(
    consumption_client: ConsumptionManagementClient,
    order_id: str,
    reservation_id: str,
) -> tuple[Optional[float], Optional[float]]:
    """Return (last_day, seven_day) utilization percentages.

    Uses the Azure Consumption "Reservation Summaries" API (daily grain):
        GET /providers/Microsoft.Capacity/reservationorders/{orderId}/
            reservations/{reservationId}/providers/Microsoft.Consumption/
            reservationSummaries?grain=daily

    - last_day: avg utilization of the most recent daily summary.
    - seven_day: mean of the daily avg utilization over the last N days.
    """
    summaries = list(
        consumption_client.reservations_summaries.list_by_reservation_order_and_reservation(
            reservation_order_id=order_id,
            reservation_id=reservation_id,
            grain="daily",
        )
    )
    if not summaries:
        return None, None

    # The API returns summaries newest-first.
    daily_avgs = [a for a in (_summary_avg(s) for s in summaries) if a is not None]
    if not daily_avgs:
        return None, None

    last_day = daily_avgs[0]
    window = daily_avgs[:UTILIZATION_DAYS]
    seven_day = sum(window) / len(window)
    return last_day, seven_day


# --- Threshold evaluation ----------------------------------------------------
def evaluate_status(
    last_day: Optional[float],
    seven_day: Optional[float],
    threshold: float,
) -> str:
    """Return HEALTHY / UNDERUTILIZED / NO DATA based on the threshold."""
    if last_day is None and seven_day is None:
        return "NO DATA"
    if (last_day is not None and last_day < threshold) or \
       (seven_day is not None and seven_day < threshold):
        return "UNDERUTILIZED"
    return "HEALTHY"


# --- Report generation -------------------------------------------------------
def _fmt_pct(value: Optional[float]) -> str:
    """Format a percentage value for display."""
    return f"{value:.0f}%" if value is not None else "-"


def build_report(records: list[ReservationRecord]) -> str:
    """Render a structured, aligned table of reservation utilization."""
    header = ["Name", "Product", "Region", "Qty", "Last Day", "7-Day", "Threshold", "Status"]
    rows = [
        [
            r.name,
            r.product,
            r.region,
            str(r.quantity),
            _fmt_pct(r.last_day),
            _fmt_pct(r.seven_day),
            f"{UTILIZATION_THRESHOLD:.0f}%",
            r.health,
        ]
        for r in records
    ]

    # Compute column widths.
    widths = [
        max(len(header[i]), *(len(row[i]) for row in rows))
        for i in range(len(header))
    ]

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [
        "Azure Reservation Utilization Report",
        "=" * (sum(widths) + 3 * (len(widths) - 1)),
        fmt_row(header),
        "-" * (sum(widths) + 3 * (len(widths) - 1)),
    ]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def build_slack_message(records: list[ReservationRecord]) -> str:
    """Build the Slack message containing the full report table.

    The table is wrapped in a code block so the monospace alignment is
    preserved in Slack. A short summary header is prepended.
    """
    today = datetime.now(timezone.utc).strftime("%d-%b-%Y")
    underutilized = [r for r in records if r.health == "UNDERUTILIZED"]
    healthy = sum(1 for r in records if r.health == "HEALTHY")

    if underutilized:
        header = (
            f"🚨 *Azure Reservation Utilization Report*\n"
            f"Date: {today}  |  Threshold: {UTILIZATION_THRESHOLD:.0f}%\n"
            f"🟢 Healthy: {healthy}  🔴 Underutilized: {len(underutilized)}"
        )
    else:
        header = (
            f"✅ *Azure Reservation Utilization Report*\n"
            f"Date: {today}  |  Threshold: {UTILIZATION_THRESHOLD:.0f}%\n"
            f"All {len(records)} reservations are above the threshold."
        )

    # Full report table in a code block to preserve column alignment.
    return f"{header}\n```\n{build_report(records)}\n```"


# --- Alerting ----------------------------------------------------------------
def send_slack(text: str) -> None:
    """Post the report to Slack via an incoming webhook."""
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL not set — skipping Slack alert.")
        return
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=30)
        response.raise_for_status()
        logger.info("Slack alert sent.")
    except requests.RequestException as exc:
        logger.error("Failed to send Slack alert: %s", exc)


# --- Main --------------------------------------------------------------------
def main() -> int:
    # 1. Authentication.
    try:
        credential = get_credential()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    # 2. Reservation discovery (Reservations API).
    reservations_client = ReservationsMgmtClient(credential)
    try:
        reservations = discover_reservations(reservations_client)
    except ClientAuthenticationError:
        logger.error("Authentication failed: invalid or expired credentials.")
        return 1
    except HttpResponseError as exc:
        if exc.status_code == 403:
            logger.error(
                "Insufficient permissions: grant the service principal Reader "
                "on the subscription(s)."
            )
        else:
            logger.error("Reservations API error: %s", exc)
        return 1

    if not reservations:
        logger.warning("No reservations found for this service principal.")
        # Notify Slack so the empty state is visible instead of silently passing.
        today = datetime.now(timezone.utc).strftime("%d-%b-%Y")
        send_slack(
            "⚠️ *Azure Reservation Utilization Report*\n"
            f"Date: {today}\n"
            "No reservations found for this service principal."
        )
        return 0

    logger.info("Discovered %d reservation(s).", len(reservations))

    # 3. Utilization retrieval (Consumption API) + threshold evaluation.
    consumption_client = ConsumptionManagementClient(credential, SUBSCRIPTION_ID)
    records: list[ReservationRecord] = []

    for reservation in reservations:
        record = build_record(reservation)
        try:
            order_id, reservation_id = get_reservation_ids(reservation)
            record.last_day, record.seven_day = get_utilization(
                consumption_client, order_id, reservation_id
            )
        except HttpResponseError as exc:
            if exc.status_code == 429:
                record.error = "throttled (429)"
                logger.warning("Throttled fetching utilization for %s", record.name)
            elif exc.status_code == 403:
                record.error = "permission denied"
                logger.warning("Permission denied fetching utilization for %s", record.name)
            else:
                record.error = str(exc)
                logger.warning("API error for %s: %s", record.name, exc)
        except Exception as exc:  # noqa: BLE001 - keep going on per-item errors
            record.error = str(exc)
            logger.warning("Failed to fetch utilization for %s: %s", record.name, exc)

        record.health = evaluate_status(record.last_day, record.seven_day, UTILIZATION_THRESHOLD)
        records.append(record)

    # 4. Report generation.
    print(build_report(records))

    # 5. Alerting.
    underutilized = [r for r in records if r.health == "UNDERUTILIZED"]
    if ALERT_ONLY_UNDERUTILIZED and not underutilized:
        logger.info("All reservations healthy — no alert sent.")
        return 0

    send_slack(build_slack_message(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())