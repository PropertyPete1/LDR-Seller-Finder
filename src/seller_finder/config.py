"""Central configuration for LDR-Seller-Finder.

All secrets come from environment variables (GitHub Actions secrets).
Non-secret tunables live in config/settings.yaml and can be edited without
touching code.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# ── Paths ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "seller_finder.sqlite3")))
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", str(REPO_ROOT / "config" / "settings.yaml")))
REVIEW_DIR = Path(os.environ.get("REVIEW_DIR", str(REPO_ROOT / "review")))

# ── Timezone ─────────────────────────────────────────────────────────────
CT = ZoneInfo("America/Chicago")

# ── Secrets (GitHub Actions secrets → env) ───────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FUB_API_KEY = os.environ.get("FUB_API_KEY", "")
BATCHDATA_API_KEY = os.environ.get("BATCHDATA_API_KEY", "")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

# GitHub Actions sets env vars to "" when the secret is missing, so treat
# empty strings as unset and fall back to sensible defaults.
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD", _env("SMTP_PASS"))
EMAIL_FROM = _env("EMAIL_FROM", SMTP_USER)
OWNER_EMAIL = _env("OWNER_EMAIL", "peter@lifestyledesignrealty.com")

# ── Behavior flags ───────────────────────────────────────────────────────
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
# daily = light run (new pre-foreclosure only); weekly = full pipeline.
RUN_MODE = (os.environ.get("RUN_MODE") or "weekly").strip().lower()
if RUN_MODE not in ("daily", "weekly"):
    RUN_MODE = "weekly"


def load_settings() -> dict:
    """Load non-secret tunables from config/settings.yaml."""
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


SETTINGS = load_settings()

# Scoring weights (0-100 scale). Overridable in settings.yaml.
SCORING = SETTINGS.get("scoring", {})
SCORE_ABSENTEE = SCORING.get("absentee_owner", 30)
SCORE_DIVORCE = SCORING.get("divorce_match", 25)
SCORE_PREFORECLOSURE = SCORING.get("preforeclosure_match", 30)
SCORE_LONG_OWNERSHIP = SCORING.get("owned_10_plus_years", 20)
SCORE_HOMESTEAD_REMOVED = SCORING.get("homestead_removed", 10)
SCORE_THRESHOLD = SCORING.get("skip_trace_threshold", 40)
# Warm tier: scored and stored (compact), NEVER traced or pushed (zero
# spend). Auto-promotes to qualified when new signals lift the score.
WARM_TIER_MIN = SCORING.get("warm_tier_min", 30)

# Counties enabled for this deployment. Add new counties in settings.yaml.
COUNTIES = SETTINGS.get("counties", ["bexar", "comal"])

# Skip-trace budget guard: max new (never-traced) owners per run, resolved
# by run mode (settings.skip_trace_budget: {weekly: 75, daily: 15} ≈
# $50-90/month at $0.15/trace). Env MAX_SKIP_TRACES_PER_RUN overrides both.
_BUDGETS = SETTINGS.get("skip_trace_budget", {}) or {}
_DEFAULT_BUDGET = {"weekly": 75, "daily": 15}
MAX_SKIP_TRACES_PER_RUN = int(
    os.environ.get("MAX_SKIP_TRACES_PER_RUN")
    or _BUDGETS.get(RUN_MODE, _DEFAULT_BUDGET[RUN_MODE])
)

# Estimated cost per successful skip-trace (for spend reporting only).
SKIP_TRACE_COST_USD = float(SETTINGS.get("skip_trace_cost_usd", 0.15))

# FUB tags
TAG_SELLER = "Seller Lead"
TAG_BY_SOURCE = {
    "absentee": "County-Absentee",
    "divorce": "Divorce-Filing",
    "preforeclosure": "Pre-Foreclosure",
}
