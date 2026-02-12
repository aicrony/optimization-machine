"""
Bot Detection Utilities for Churn Analysis

Identifies bot accounts to filter them from churn analysis,
ensuring reports reflect real customer behavior.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Known bot user-agent patterns
BOT_USER_AGENT_PATTERNS = [
    # Headless browsers and automation
    r'headlesschrome',
    r'phantomjs',
    r'selenium',
    r'puppeteer',
    r'playwright',
    r'webdriver',

    # Search engine bots
    r'googlebot',
    r'bingbot',
    r'slurp',
    r'duckduckbot',
    r'baiduspider',
    r'yandexbot',
    r'sogou',
    r'exabot',
    r'facebot',
    r'ia_archiver',

    # HTTP clients and scrapers
    r'curl[\/\s]',
    r'wget[\/\s]',
    r'python-requests',
    r'python-urllib',
    r'axios',
    r'node-fetch',
    r'go-http-client',
    r'java[\/\s]',
    r'libwww',
    r'lwp-trivial',
    r'httplib',
    r'httpclient',

    # Scraping frameworks
    r'scrapy',
    r'crawler',
    r'spider',
    r'scraper',

    # Generic bot patterns
    r'\bbot\b',
    r'\bcrawl',
]

# Disposable email domain patterns
DISPOSABLE_EMAIL_DOMAINS = [
    'tempmail', 'temp-mail', 'throwaway', 'guerrilla',
    'mailinator', 'yopmail', 'sharklasers', 'guerrillamail',
    'fakeinbox', 'trashmail', 'getairmail', 'discard',
    '10minutemail', 'minutemail', 'tempinbox', 'tempail',
]

# Compile patterns for efficiency
_BOT_UA_REGEX = re.compile('|'.join(BOT_USER_AGENT_PATTERNS), re.IGNORECASE)
_DISPOSABLE_EMAIL_REGEX = re.compile(
    '|'.join(rf'@[^@]*{domain}' for domain in DISPOSABLE_EMAIL_DOMAINS),
    re.IGNORECASE
)


@dataclass
class BotDetectionResult:
    """Result of bot detection analysis"""
    is_bot: bool
    confidence: float  # 0.0 to 1.0
    reasons: List[str]

    def __str__(self) -> str:
        if not self.is_bot:
            return "Not a bot"
        return f"Bot ({self.confidence:.0%} confidence): {', '.join(self.reasons)}"


@dataclass
class BotDetectionConfig:
    """Configuration for bot detection thresholds"""
    # Behavioral thresholds
    min_account_age_hours: int = 1  # Accounts younger than this are suspicious
    min_generations_for_paid: int = 1  # Paid users with 0 generations are suspicious
    max_generations_per_hour: int = 100  # Unrealistic generation rate

    # Scoring weights (should sum to ~1.0 for intuitive confidence)
    user_agent_weight: float = 0.4
    email_weight: float = 0.2
    behavior_weight: float = 0.4

    # Final threshold
    bot_threshold: float = 0.5  # Score >= this = flagged as bot


def detect_bot_by_user_agent(user_agent: Optional[str]) -> Tuple[bool, float, str]:
    """
    Check if user agent matches known bot patterns.

    Returns:
        (is_bot, confidence, reason)
    """
    if not user_agent:
        return False, 0.0, ""

    user_agent_lower = user_agent.lower()

    # Check for known bot patterns
    match = _BOT_UA_REGEX.search(user_agent_lower)
    if match:
        matched_pattern = match.group()
        return True, 0.9, f"Bot user-agent pattern: '{matched_pattern}'"

    # Check for missing or suspicious user agents
    if len(user_agent) < 10:
        return True, 0.6, "Suspiciously short user-agent"

    # Check for common browser indicators (absence is suspicious)
    browser_indicators = ['mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera']
    has_browser = any(indicator in user_agent_lower for indicator in browser_indicators)
    if not has_browser:
        return True, 0.5, "No browser signature in user-agent"

    return False, 0.0, ""


def detect_bot_by_email(email: Optional[str]) -> Tuple[bool, float, str]:
    """
    Check if email matches disposable/suspicious patterns.

    Returns:
        (is_bot, confidence, reason)
    """
    if not email:
        return False, 0.0, ""

    email_lower = email.lower()

    # Check for disposable email domains
    if _DISPOSABLE_EMAIL_REGEX.search(email_lower):
        return True, 0.8, "Disposable email domain"

    # Check for suspicious patterns
    # Random string usernames (lots of numbers, no vowels, etc.)
    username = email_lower.split('@')[0]

    # Count digits in username
    digit_ratio = sum(c.isdigit() for c in username) / max(len(username), 1)
    if digit_ratio > 0.6 and len(username) > 8:
        return True, 0.4, "Suspicious email pattern (high digit ratio)"

    return False, 0.0, ""


def detect_bot_by_behavior(
    account_created: Optional[datetime],
    subscription_start: Optional[datetime],
    cancellation_date: Optional[datetime],
    total_generations: int = 0,
    successful_generations: int = 0,
    failed_generations: int = 0,
    config: Optional[BotDetectionConfig] = None
) -> Tuple[bool, float, str]:
    """
    Check for bot-like behavioral patterns.

    Returns:
        (is_bot, confidence, reason)
    """
    if config is None:
        config = BotDetectionConfig()

    reasons = []
    max_confidence = 0.0

    # Check account age at subscription
    if account_created and subscription_start:
        time_to_subscribe = (subscription_start - account_created).total_seconds()
        if time_to_subscribe < 60:  # Subscribed within 1 minute of account creation
            reasons.append("Subscribed within 1 minute of signup")
            max_confidence = max(max_confidence, 0.7)

    # Check subscription duration
    if subscription_start and cancellation_date:
        duration = cancellation_date - subscription_start
        if duration < timedelta(hours=1):
            reasons.append("Subscription lasted < 1 hour")
            max_confidence = max(max_confidence, 0.8)
        elif duration < timedelta(hours=24):
            reasons.append("Subscription lasted < 24 hours")
            max_confidence = max(max_confidence, 0.5)

    # Check generation activity
    if total_generations == 0:
        reasons.append("Zero generations during subscription")
        max_confidence = max(max_confidence, 0.6)
    elif failed_generations > 0 and successful_generations == 0:
        reasons.append("Only failed generations (no successful usage)")
        max_confidence = max(max_confidence, 0.5)

    # Check for unrealistic generation rates
    if subscription_start and cancellation_date and total_generations > 0:
        duration_hours = max((cancellation_date - subscription_start).total_seconds() / 3600, 0.1)
        rate = total_generations / duration_hours
        if rate > config.max_generations_per_hour:
            reasons.append(f"Unrealistic generation rate: {rate:.0f}/hour")
            max_confidence = max(max_confidence, 0.7)

    is_bot = max_confidence >= 0.5
    reason_str = "; ".join(reasons) if reasons else ""

    return is_bot, max_confidence, reason_str


def calculate_composite_bot_score(
    user_agent_result: Tuple[bool, float, str],
    email_result: Tuple[bool, float, str],
    behavior_result: Tuple[bool, float, str],
    config: Optional[BotDetectionConfig] = None
) -> BotDetectionResult:
    """
    Combine all detection methods into a composite score.

    Returns:
        BotDetectionResult with final verdict
    """
    if config is None:
        config = BotDetectionConfig()

    # Extract confidences
    ua_is_bot, ua_conf, ua_reason = user_agent_result
    email_is_bot, email_conf, email_reason = email_result
    behav_is_bot, behav_conf, behav_reason = behavior_result

    # Calculate weighted score
    weighted_score = (
        (ua_conf * config.user_agent_weight) +
        (email_conf * config.email_weight) +
        (behav_conf * config.behavior_weight)
    )

    # Collect all reasons
    reasons = []
    if ua_reason:
        reasons.append(ua_reason)
    if email_reason:
        reasons.append(email_reason)
    if behav_reason:
        reasons.append(behav_reason)

    # High confidence from any single source should flag as bot
    any_high_confidence = max(ua_conf, email_conf, behav_conf) >= 0.8

    is_bot = weighted_score >= config.bot_threshold or any_high_confidence

    return BotDetectionResult(
        is_bot=is_bot,
        confidence=min(weighted_score, 1.0),
        reasons=reasons
    )


def analyze_user_for_bot(
    user_data: Dict[str, Any],
    activity_data: Optional[Dict[str, Any]] = None,
    config: Optional[BotDetectionConfig] = None
) -> BotDetectionResult:
    """
    Comprehensive bot analysis for a single user.

    Args:
        user_data: Dict with keys like 'email', 'user_agent', 'created_at'
        activity_data: Dict with keys like 'total_generations', 'successful_generations'
        config: Detection configuration

    Returns:
        BotDetectionResult
    """
    if config is None:
        config = BotDetectionConfig()

    if activity_data is None:
        activity_data = {}

    # Parse dates if they're strings, always return naive UTC datetime
    def parse_date(val):
        if val is None:
            return None
        if isinstance(val, datetime):
            # Convert to naive UTC if timezone-aware
            if val.tzinfo is not None:
                from datetime import timezone
                return val.astimezone(timezone.utc).replace(tzinfo=None)
            return val
        try:
            # Handle ISO format with or without timezone
            dt_str = str(val).replace('Z', '+00:00')
            dt = datetime.fromisoformat(dt_str)
            # Convert to naive UTC
            if dt.tzinfo is not None:
                from datetime import timezone
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, AttributeError):
            return None

    # Run detection methods
    ua_result = detect_bot_by_user_agent(user_data.get('user_agent'))
    email_result = detect_bot_by_email(user_data.get('email'))
    behavior_result = detect_bot_by_behavior(
        account_created=parse_date(user_data.get('created_at')),
        subscription_start=parse_date(user_data.get('subscription_start')),
        cancellation_date=parse_date(user_data.get('cancellation_date')),
        total_generations=activity_data.get('total_generations', 0),
        successful_generations=activity_data.get('successful_generations', 0),
        failed_generations=activity_data.get('failed_generations', 0),
        config=config
    )

    return calculate_composite_bot_score(ua_result, email_result, behavior_result, config)


def filter_bots_from_users(
    users: List[Dict[str, Any]],
    activity_by_user: Optional[Dict[str, Dict[str, Any]]] = None,
    config: Optional[BotDetectionConfig] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, BotDetectionResult]]:
    """
    Filter a list of users, separating humans from bots.

    Args:
        users: List of user dicts
        activity_by_user: Dict mapping user_id to activity data
        config: Detection configuration

    Returns:
        (human_users, bot_users, detection_results_by_user_id)
    """
    if activity_by_user is None:
        activity_by_user = {}

    humans = []
    bots = []
    results = {}

    for user in users:
        user_id = user.get('id') or user.get('user_id')
        activity = activity_by_user.get(user_id, {})

        result = analyze_user_for_bot(user, activity, config)
        results[user_id] = result

        if result.is_bot:
            bots.append(user)
        else:
            humans.append(user)

    logger.info(f"Bot filtering: {len(humans)} humans, {len(bots)} bots from {len(users)} users")

    return humans, bots, results
