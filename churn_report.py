"""
Churn Analysis Report Generator

Generates comprehensive churn analysis reports with:
- Aggregate churn trends
- User-level churn details with activity correlation
- Bot filtering for clean data
- Friction point analysis from Clarity

Usage:
    CLI: python main.py churn-report [--days 30] [--verbose]
    Module: from churn_report import generate_churn_report
"""

import os
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

import stripe
from dotenv import load_dotenv

from db_handler import get_db_handler
from data_collector import get_data_collector
from bot_detector import (
    filter_bots_from_users,
    BotDetectionConfig,
    BotDetectionResult
)

load_dotenv('config.env')

logger = logging.getLogger(__name__)

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_API_KEY')


@dataclass
class ChurnedUser:
    """Represents a churned user with full context"""
    user_id: str
    email: Optional[str]
    stripe_customer_id: str
    subscription_id: str
    subscription_start: datetime
    cancellation_date: datetime
    tenure_days: int
    tenure_segment: str  # '<7d', '7-30d', '30+d'
    mrr_lost: float
    plan_name: Optional[str]
    cancellation_reason: Optional[str]
    # Activity metrics
    total_generations: int = 0
    successful_generations: int = 0
    failed_generations: int = 0
    last_activity: Optional[datetime] = None
    # Bot detection
    is_bot: bool = False
    bot_reasons: List[str] = field(default_factory=list)


@dataclass
class ChurnSummary:
    """Summary statistics for churn analysis"""
    period_days: int
    report_date: datetime
    # Aggregate metrics
    total_churned: int
    total_churned_filtered: int  # After bot filtering
    bots_filtered: int
    total_mrr_lost: float
    mrr_lost_filtered: float
    avg_tenure_days: float
    # Churn rate context
    current_churn_rate: Optional[float]
    previous_churn_rate: Optional[float]
    churn_rate_change: Optional[float]
    # By tenure segment
    segment_breakdown: Dict[str, Dict[str, Any]]
    # Top cancellation reasons
    cancellation_reasons: Dict[str, int]


@dataclass
class UpcomingCancellation:
    """Represents a user scheduled to cancel (future churn)"""
    user_id: str
    email: Optional[str]
    stripe_customer_id: str
    subscription_id: str
    subscription_start: datetime
    cancel_at: datetime  # When the subscription will end
    days_until_cancel: int
    current_mrr: float
    plan_name: Optional[str]
    tenure_days: int  # How long they've been subscribed
    # Activity metrics
    total_generations: int = 0
    successful_generations: int = 0
    failed_generations: int = 0
    last_activity: Optional[datetime] = None
    # Retention opportunity
    retention_priority: str = 'medium'  # 'high', 'medium', 'low'
    retention_notes: List[str] = field(default_factory=list)


@dataclass
class ChurnReport:
    """Complete churn analysis report"""
    summary: ChurnSummary
    churned_users: List[ChurnedUser]
    bot_users: List[ChurnedUser]
    upcoming_cancellations: List[UpcomingCancellation]  # Future churn
    friction_data: Dict[str, Any]
    historical_metrics: List[Dict[str, Any]]
    generated_at: datetime = field(default_factory=datetime.utcnow)


def get_cancelled_subscriptions(days: int = 30) -> List[Dict[str, Any]]:
    """
    Fetch cancelled subscriptions from Stripe.

    Args:
        days: Number of days to look back

    Returns:
        List of subscription data dicts
    """
    logger.info(f"Fetching cancelled subscriptions from Stripe (last {days} days)...")

    cancelled_subs = []
    cutoff_timestamp = int((datetime.utcnow() - timedelta(days=days)).timestamp())

    try:
        # Fetch cancelled subscriptions
        # Note: Stripe doesn't have a direct "cancelled after" filter,
        # so we fetch recent cancellations and filter by date
        subscriptions = stripe.Subscription.list(
            status='canceled',
            limit=100,
            expand=['data.customer', 'data.plan']
        )

        for sub in subscriptions.auto_paging_iter():
            # Check if cancellation is within our date range
            canceled_at = sub.canceled_at or sub.ended_at
            if canceled_at and canceled_at >= cutoff_timestamp:
                # Calculate subscription details
                start_date = datetime.fromtimestamp(sub.start_date) if sub.start_date else None
                cancel_date = datetime.fromtimestamp(canceled_at)

                # Get MRR from the plan
                mrr = 0.0
                plan_name = None
                if sub.plan:
                    if sub.plan.interval == 'month':
                        mrr = sub.plan.amount / 100
                    elif sub.plan.interval == 'year':
                        mrr = (sub.plan.amount / 100) / 12
                    plan_name = sub.plan.nickname or sub.plan.id

                # Get customer email
                customer_email = None
                if hasattr(sub.customer, 'email'):
                    customer_email = sub.customer.email
                elif isinstance(sub.customer, str):
                    # Customer is just an ID, need to fetch
                    try:
                        cust = stripe.Customer.retrieve(sub.customer)
                        customer_email = cust.email
                    except Exception:
                        pass

                cancelled_subs.append({
                    'subscription_id': sub.id,
                    'stripe_customer_id': sub.customer.id if hasattr(sub.customer, 'id') else sub.customer,
                    'customer_email': customer_email,
                    'start_date': start_date,
                    'canceled_at': cancel_date,
                    'mrr': mrr,
                    'plan_name': plan_name,
                    'cancellation_reason': sub.cancellation_details.reason if sub.cancellation_details else None,
                    'cancel_feedback': sub.cancellation_details.feedback if sub.cancellation_details else None,
                })

        logger.info(f"Found {len(cancelled_subs)} cancelled subscriptions in the last {days} days")

    except stripe.error.StripeError as e:
        logger.error(f"Stripe API error: {e}")
    except Exception as e:
        logger.error(f"Error fetching cancelled subscriptions: {e}")

    return cancelled_subs


def get_upcoming_cancellations() -> List[Dict[str, Any]]:
    """
    Fetch subscriptions scheduled to cancel (cancel_at_period_end=true).
    These are users who have requested cancellation but are still active.

    Returns:
        List of subscription data dicts for upcoming cancellations
    """
    logger.info("Fetching upcoming cancellations from Stripe...")

    upcoming = []

    try:
        # Fetch active subscriptions that are set to cancel at period end
        subscriptions = stripe.Subscription.list(
            status='active',
            limit=100,
            expand=['data.customer', 'data.plan']
        )

        now = datetime.utcnow()

        for sub in subscriptions.auto_paging_iter():
            # Check if subscription is set to cancel
            if sub.cancel_at_period_end and sub.cancel_at:
                cancel_date = datetime.fromtimestamp(sub.cancel_at)
                start_date = datetime.fromtimestamp(sub.start_date) if sub.start_date else None

                # Calculate days until cancellation
                days_until = (cancel_date - now).days

                # Get MRR from the plan
                mrr = 0.0
                plan_name = None
                if sub.plan:
                    if sub.plan.interval == 'month':
                        mrr = sub.plan.amount / 100
                    elif sub.plan.interval == 'year':
                        mrr = (sub.plan.amount / 100) / 12
                    plan_name = sub.plan.nickname or sub.plan.id

                # Get customer email
                customer_email = None
                if hasattr(sub.customer, 'email'):
                    customer_email = sub.customer.email
                elif isinstance(sub.customer, str):
                    try:
                        cust = stripe.Customer.retrieve(sub.customer)
                        customer_email = cust.email
                    except Exception:
                        pass

                # Calculate tenure
                tenure_days = (now - start_date).days if start_date else 0

                upcoming.append({
                    'subscription_id': sub.id,
                    'stripe_customer_id': sub.customer.id if hasattr(sub.customer, 'id') else sub.customer,
                    'customer_email': customer_email,
                    'start_date': start_date,
                    'cancel_at': cancel_date,
                    'days_until_cancel': days_until,
                    'mrr': mrr,
                    'plan_name': plan_name,
                    'tenure_days': tenure_days,
                })

        logger.info(f"Found {len(upcoming)} upcoming cancellations")

    except stripe.error.StripeError as e:
        logger.error(f"Stripe API error: {e}")
    except Exception as e:
        logger.error(f"Error fetching upcoming cancellations: {e}")

    return upcoming


def analyze_retention_opportunity(
    user: 'UpcomingCancellation',
    activity: Dict[str, Any]
) -> Tuple[str, List[str]]:
    """
    Analyze retention opportunity for a user scheduled to cancel.

    Returns:
        (priority, notes) where priority is 'high', 'medium', or 'low'
    """
    notes = []
    score = 50  # Start at medium

    # High value customer (MRR > $30)
    if user.current_mrr >= 30:
        score += 20
        notes.append(f"High-value customer (${user.current_mrr:.0f}/mo)")

    # Long tenure (loyal customer)
    if user.tenure_days >= 90:
        score += 15
        notes.append(f"Loyal customer ({user.tenure_days} days)")
    elif user.tenure_days < 14:
        score -= 10
        notes.append("Very new customer - may be testing")

    # Active user (has generations)
    total_gens = activity.get('total_generations', 0)
    if total_gens > 10:
        score += 20
        notes.append(f"Active user ({total_gens} generations)")
    elif total_gens == 0:
        score -= 15
        notes.append("Never used the product - onboarding issue?")

    # Recent activity
    last_activity = activity.get('last_activity')
    if last_activity:
        days_since_active = (datetime.utcnow() - last_activity).days
        if days_since_active < 7:
            score += 10
            notes.append("Recently active")
        elif days_since_active > 30:
            score -= 10
            notes.append(f"Inactive for {days_since_active} days")

    # Canceling soon (urgent)
    if user.days_until_cancel <= 3:
        score += 10
        notes.append("Cancels very soon - urgent!")
    elif user.days_until_cancel <= 7:
        notes.append("Cancels within a week")

    # Determine priority
    if score >= 70:
        priority = 'high'
    elif score >= 40:
        priority = 'medium'
    else:
        priority = 'low'

    return priority, notes


def get_customers_mapping(db_handler) -> Dict[str, str]:
    """
    Get mapping of stripe_customer_id -> supabase_user_id from public.customers.

    Returns:
        Dict mapping stripe_customer_id to user_id
    """
    logger.info("Fetching customer mapping from Supabase...")

    mapping = {}

    try:
        # Query the GenTube Supabase (auth instance)
        collector = get_data_collector()
        if not collector.supabase_auth:
            logger.warning("Supabase auth client not available")
            return mapping

        # Query public.customers table
        response = collector.supabase_auth.table('customers').select(
            'id, stripe_customer_id'
        ).execute()

        if response.data:
            for row in response.data:
                if row.get('stripe_customer_id'):
                    mapping[row['stripe_customer_id']] = row['id']

        logger.info(f"Loaded {len(mapping)} customer mappings")

    except Exception as e:
        logger.error(f"Error fetching customer mapping: {e}")

    return mapping


def get_user_details(db_handler, user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Get user details (email, created_at, etc.) for a list of user IDs.

    Returns:
        Dict mapping user_id to user details
    """
    if not user_ids:
        return {}

    logger.info(f"Fetching details for {len(user_ids)} users...")

    users = {}

    try:
        collector = get_data_collector()
        if not collector.supabase_auth:
            return users

        # Query in batches to avoid hitting limits
        batch_size = 50
        for i in range(0, len(user_ids), batch_size):
            batch = user_ids[i:i + batch_size]

            # Query auth.users for user details using the auth admin API
            # Note: This requires service_role key with appropriate permissions
            try:
                # Use the auth schema (auth.users) via RPC or direct query
                # The supabase-py client with service_role key can access auth.users
                response = collector.supabase_auth.from_('auth.users').select(
                    'id, email, created_at, raw_user_meta_data'
                ).in_('id', batch).execute()

                if response.data:
                    for row in response.data:
                        users[row['id']] = {
                            'id': row['id'],
                            'email': row.get('email'),
                            'created_at': row.get('created_at'),
                            'user_agent': row.get('raw_user_meta_data', {}).get('user_agent') if row.get('raw_user_meta_data') else None
                        }
            except Exception as e:
                # Fallback: try using the admin API for auth users
                logger.debug(f"Direct auth.users query failed, trying admin API: {e}")
                try:
                    # Use Supabase Admin API to list users
                    for user_id in batch:
                        try:
                            user_response = collector.supabase_auth.auth.admin.get_user_by_id(user_id)
                            if user_response and user_response.user:
                                user = user_response.user
                                users[user.id] = {
                                    'id': user.id,
                                    'email': user.email,
                                    'created_at': user.created_at,
                                    'user_agent': user.user_metadata.get('user_agent') if user.user_metadata else None
                                }
                        except Exception:
                            pass
                except Exception as admin_err:
                    logger.warning(f"Error fetching user batch via admin API: {admin_err}")

    except Exception as e:
        logger.error(f"Error fetching user details: {e}")

    return users


def get_user_activity(collector, user_ids: List[str], days: int = 90) -> Dict[str, Dict[str, Any]]:
    """
    Get generation activity for users from Google Cloud Datastore.

    Returns:
        Dict mapping user_id to activity metrics
    """
    if not user_ids or not collector.datastore_client:
        return {}

    logger.info(f"Fetching activity for {len(user_ids)} users from Datastore...")

    # Convert user_ids to a set for fast lookup
    user_ids_set = set(user_ids) - collector.gcd_excluded_user_ids

    # Initialize activity dict for all requested users
    activity = {uid: {
        'total_generations': 0,
        'successful_generations': 0,
        'failed_generations': 0,
        'last_activity': None
    } for uid in user_ids_set}

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    SUCCESS_TYPES = {'img', 'vid', 'video', 'image', 'audio', 'music', 'faceswap'}
    FAILURE_TYPES = {'err', 'image-failed'}
    EXCLUDED_TYPES = {'que', 'upl'}

    try:
        from google.cloud import datastore

        # Query all UserActivity for the date range, then filter by user in Python
        # This is necessary because UserId may be stored as a list type in Datastore
        query = collector.datastore_client.query(kind='UserActivity')
        query.add_filter(filter=datastore.query.PropertyFilter('DateTime', '>=', cutoff))

        for entity in query.fetch():
            # Handle UserId which may be stored as a list
            entity_user_id = entity.get('UserId')
            if isinstance(entity_user_id, list):
                entity_user_id = entity_user_id[0] if entity_user_id else None
            entity_user_id = str(entity_user_id) if entity_user_id else None

            # Skip if not in our target user list
            if not entity_user_id or entity_user_id not in user_ids_set:
                continue

            activity_type = entity.get('AssetType', '')

            # Skip excluded types
            if activity_type in EXCLUDED_TYPES:
                continue

            # Count by type
            if activity_type in SUCCESS_TYPES:
                activity[entity_user_id]['successful_generations'] += 1
                activity[entity_user_id]['total_generations'] += 1
            elif activity_type in FAILURE_TYPES:
                activity[entity_user_id]['failed_generations'] += 1
                activity[entity_user_id]['total_generations'] += 1

            # Track last activity
            created_at = entity.get('DateTime')
            if isinstance(created_at, str):
                try:
                    parsed = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    created_at = parsed.replace(tzinfo=None)
                except (ValueError, AttributeError):
                    created_at = None

            if created_at:
                current_last = activity[entity_user_id]['last_activity']
                if current_last is None or created_at > current_last:
                    activity[entity_user_id]['last_activity'] = created_at

    except ImportError:
        logger.warning("Google Cloud Datastore not available")
    except Exception as e:
        logger.error(f"Error fetching user activity: {e}")

    # Count users with actual activity
    users_with_activity = sum(1 for u in activity.values() if u['total_generations'] > 0)
    logger.info(f"Retrieved activity for {users_with_activity}/{len(activity)} users")
    return activity


def get_historical_metrics(db_handler, days: int = 30) -> List[Dict[str, Any]]:
    """
    Get historical metrics for trend analysis.

    Returns:
        List of metric records ordered by timestamp
    """
    logger.info(f"Fetching historical metrics (last {days} days)...")

    try:
        metrics = db_handler.get_latest_metrics(limit=days * 2)  # ~2 per day buffer
        return sorted(metrics, key=lambda x: x.get('timestamp', ''))
    except Exception as e:
        logger.error(f"Error fetching historical metrics: {e}")
        return []


def get_clarity_friction_data(collector) -> Dict[str, Any]:
    """
    Get Clarity UX friction data (uses cached data from regular collection cycle).

    Returns:
        Dict with friction metrics and page breakdown
    """
    logger.info("Fetching Clarity friction data (from cache)...")

    try:
        # Get cached Clarity data from settings
        db = get_db_handler()
        cached_raw = db.get_setting('clarity_cache')

        if cached_raw:
            cached = json.loads(cached_raw)
            cache_age = (datetime.utcnow() - datetime.fromisoformat(cached['timestamp'])).total_seconds() / 3600
            logger.info(f"Using Clarity cache ({cache_age:.1f}h old)")
            return cached['data']

    except Exception as e:
        logger.warning(f"Error fetching Clarity cache: {e}")

    return {}


def calculate_tenure_segment(tenure_days: int) -> str:
    """Categorize tenure into segments."""
    if tenure_days < 7:
        return '<7d'
    elif tenure_days <= 30:
        return '7-30d'
    else:
        return '30+d'


def build_churned_users(
    cancelled_subs: List[Dict[str, Any]],
    customer_mapping: Dict[str, str],
    user_details: Dict[str, Dict[str, Any]],
    user_activity: Dict[str, Dict[str, Any]]
) -> List[ChurnedUser]:
    """
    Build ChurnedUser objects from raw data.
    """
    churned_users = []

    for sub in cancelled_subs:
        stripe_id = sub['stripe_customer_id']
        user_id = customer_mapping.get(stripe_id)

        # Calculate tenure
        start_date = sub['start_date']
        cancel_date = sub['canceled_at']
        tenure_days = (cancel_date - start_date).days if start_date and cancel_date else 0

        # Get user details
        details = user_details.get(user_id, {}) if user_id else {}
        activity = user_activity.get(user_id, {}) if user_id else {}

        churned_user = ChurnedUser(
            user_id=user_id or stripe_id,  # Fall back to stripe ID if no mapping
            email=details.get('email') or sub.get('customer_email'),
            stripe_customer_id=stripe_id,
            subscription_id=sub['subscription_id'],
            subscription_start=start_date,
            cancellation_date=cancel_date,
            tenure_days=tenure_days,
            tenure_segment=calculate_tenure_segment(tenure_days),
            mrr_lost=sub.get('mrr', 0),
            plan_name=sub.get('plan_name'),
            cancellation_reason=sub.get('cancellation_reason') or sub.get('cancel_feedback'),
            total_generations=activity.get('total_generations', 0),
            successful_generations=activity.get('successful_generations', 0),
            failed_generations=activity.get('failed_generations', 0),
            last_activity=activity.get('last_activity'),
        )

        churned_users.append(churned_user)

    return churned_users


def build_upcoming_cancellations(
    upcoming_subs: List[Dict[str, Any]],
    customer_mapping: Dict[str, str],
    user_details: Dict[str, Dict[str, Any]],
    user_activity: Dict[str, Dict[str, Any]]
) -> List[UpcomingCancellation]:
    """
    Build UpcomingCancellation objects with retention analysis.
    """
    upcoming = []

    for sub in upcoming_subs:
        stripe_id = sub['stripe_customer_id']
        user_id = customer_mapping.get(stripe_id)

        # Get user details and activity
        details = user_details.get(user_id, {}) if user_id else {}
        activity = user_activity.get(user_id, {}) if user_id else {}

        upcoming_cancel = UpcomingCancellation(
            user_id=user_id or stripe_id,
            email=details.get('email') or sub.get('customer_email'),
            stripe_customer_id=stripe_id,
            subscription_id=sub['subscription_id'],
            subscription_start=sub['start_date'],
            cancel_at=sub['cancel_at'],
            days_until_cancel=sub['days_until_cancel'],
            current_mrr=sub.get('mrr', 0),
            plan_name=sub.get('plan_name'),
            tenure_days=sub.get('tenure_days', 0),
            total_generations=activity.get('total_generations', 0),
            successful_generations=activity.get('successful_generations', 0),
            failed_generations=activity.get('failed_generations', 0),
            last_activity=activity.get('last_activity'),
        )

        # Analyze retention opportunity
        priority, notes = analyze_retention_opportunity(upcoming_cancel, activity)
        upcoming_cancel.retention_priority = priority
        upcoming_cancel.retention_notes = notes

        upcoming.append(upcoming_cancel)

    # Sort by priority (high first) then by days until cancel (soonest first)
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    upcoming.sort(key=lambda x: (priority_order.get(x.retention_priority, 1), x.days_until_cancel))

    return upcoming


def apply_bot_filtering(
    churned_users: List[ChurnedUser],
    user_details: Dict[str, Dict[str, Any]],
    user_activity: Dict[str, Dict[str, Any]]
) -> Tuple[List[ChurnedUser], List[ChurnedUser]]:
    """
    Apply bot detection to churned users.

    Returns:
        (human_users, bot_users)
    """
    logger.info("Applying bot detection...")

    # Prepare data for bot detector
    users_data = []
    for user in churned_users:
        user_data = {
            'id': user.user_id,
            'email': user.email,
            'created_at': user_details.get(user.user_id, {}).get('created_at'),
            'user_agent': user_details.get(user.user_id, {}).get('user_agent'),
            'subscription_start': user.subscription_start,
            'cancellation_date': user.cancellation_date,
        }
        users_data.append(user_data)

    # Build activity mapping
    activity_mapping = {}
    for user in churned_users:
        activity_mapping[user.user_id] = {
            'total_generations': user.total_generations,
            'successful_generations': user.successful_generations,
            'failed_generations': user.failed_generations,
        }

    # Run bot detection
    humans_data, bots_data, detection_results = filter_bots_from_users(
        users_data,
        activity_mapping,
        BotDetectionConfig()
    )

    # Map results back to ChurnedUser objects
    bot_ids = {u['id'] for u in bots_data}
    humans = []
    bots = []

    for user in churned_users:
        result = detection_results.get(user.user_id)
        if user.user_id in bot_ids:
            user.is_bot = True
            user.bot_reasons = result.reasons if result else []
            bots.append(user)
        else:
            humans.append(user)

    logger.info(f"Bot detection complete: {len(humans)} humans, {len(bots)} bots")

    return humans, bots


def calculate_summary(
    period_days: int,
    humans: List[ChurnedUser],
    bots: List[ChurnedUser],
    historical_metrics: List[Dict[str, Any]]
) -> ChurnSummary:
    """
    Calculate summary statistics from churned users.
    """
    total_churned = len(humans) + len(bots)
    total_churned_filtered = len(humans)
    bots_filtered = len(bots)

    total_mrr_lost = sum(u.mrr_lost for u in humans + bots)
    mrr_lost_filtered = sum(u.mrr_lost for u in humans)

    avg_tenure = sum(u.tenure_days for u in humans) / max(len(humans), 1)

    # Segment breakdown
    segments = {'<7d': [], '7-30d': [], '30+d': []}
    for user in humans:
        segments[user.tenure_segment].append(user)

    segment_breakdown = {}
    for seg, users in segments.items():
        segment_breakdown[seg] = {
            'count': len(users),
            'mrr_lost': sum(u.mrr_lost for u in users),
            'avg_tenure': sum(u.tenure_days for u in users) / max(len(users), 1),
            'pct_of_total': len(users) / max(total_churned_filtered, 1) * 100
        }

    # Cancellation reasons
    reasons = {}
    for user in humans:
        reason = user.cancellation_reason or 'Not specified'
        reasons[reason] = reasons.get(reason, 0) + 1

    # Churn rate from historical metrics
    current_churn = None
    previous_churn = None
    if historical_metrics:
        # Most recent
        latest = historical_metrics[-1] if historical_metrics else {}
        current_churn = latest.get('churn_rate')

        # Previous period (try to find one from ~30 days ago)
        if len(historical_metrics) > 5:
            mid_index = len(historical_metrics) // 2
            previous = historical_metrics[mid_index]
            previous_churn = previous.get('churn_rate')

    churn_change = None
    if current_churn is not None and previous_churn is not None:
        churn_change = current_churn - previous_churn

    return ChurnSummary(
        period_days=period_days,
        report_date=datetime.utcnow(),
        total_churned=total_churned,
        total_churned_filtered=total_churned_filtered,
        bots_filtered=bots_filtered,
        total_mrr_lost=total_mrr_lost,
        mrr_lost_filtered=mrr_lost_filtered,
        avg_tenure_days=avg_tenure,
        current_churn_rate=current_churn,
        previous_churn_rate=previous_churn,
        churn_rate_change=churn_change,
        segment_breakdown=segment_breakdown,
        cancellation_reasons=reasons
    )


def generate_churn_report(days: int = 30, verbose: bool = False) -> ChurnReport:
    """
    Generate a complete churn analysis report.

    Args:
        days: Number of days to analyze
        verbose: Include detailed debug output

    Returns:
        ChurnReport object
    """
    logger.info(f"Generating churn report for last {days} days...")

    db = get_db_handler()
    collector = get_data_collector()

    # 1. Get cancelled subscriptions from Stripe
    cancelled_subs = get_cancelled_subscriptions(days)

    # 2. Get customer mapping from Supabase
    customer_mapping = get_customers_mapping(db)

    # 3. Get user IDs that we can look up
    stripe_ids = [sub['stripe_customer_id'] for sub in cancelled_subs]
    user_ids = [customer_mapping.get(sid) for sid in stripe_ids if customer_mapping.get(sid)]

    # 4. Get user details
    user_details = get_user_details(db, user_ids)

    # 5. Get user activity from Datastore
    # Calculate lookback based on earliest subscription start date to capture all activity
    earliest_start = None
    for sub in cancelled_subs:
        if sub.get('start_date'):
            if earliest_start is None or sub['start_date'] < earliest_start:
                earliest_start = sub['start_date']

    if earliest_start:
        activity_lookback_days = (datetime.utcnow() - earliest_start).days + 1
    else:
        activity_lookback_days = max(days, 90)

    user_activity = get_user_activity(collector, user_ids, days=activity_lookback_days)

    # 6. Build churned user objects
    churned_users = build_churned_users(
        cancelled_subs,
        customer_mapping,
        user_details,
        user_activity
    )

    # 7. Apply bot filtering
    humans, bots = apply_bot_filtering(churned_users, user_details, user_activity)

    # 8. Get Clarity friction data (cached)
    friction_data = get_clarity_friction_data(collector)

    # 9. Get historical metrics for trend analysis
    historical_metrics = get_historical_metrics(db, days)

    # 10. Calculate summary
    summary = calculate_summary(days, humans, bots, historical_metrics)

    # 11. Get upcoming cancellations (future churn)
    upcoming_subs = get_upcoming_cancellations()

    # 12. Build upcoming cancellation objects with retention analysis
    upcoming_stripe_ids = [sub['stripe_customer_id'] for sub in upcoming_subs]
    upcoming_user_ids = [customer_mapping.get(sid) for sid in upcoming_stripe_ids if customer_mapping.get(sid)]

    # Get details and activity for upcoming cancellations
    upcoming_user_details = get_user_details(db, upcoming_user_ids)

    # Calculate lookback based on earliest subscription start for upcoming cancellations
    upcoming_earliest_start = None
    for sub in upcoming_subs:
        if sub.get('start_date'):
            if upcoming_earliest_start is None or sub['start_date'] < upcoming_earliest_start:
                upcoming_earliest_start = sub['start_date']

    if upcoming_earliest_start:
        upcoming_lookback_days = (datetime.utcnow() - upcoming_earliest_start).days + 1
    else:
        upcoming_lookback_days = 90

    upcoming_user_activity = get_user_activity(collector, upcoming_user_ids, days=upcoming_lookback_days)

    upcoming_cancellations = build_upcoming_cancellations(
        upcoming_subs,
        customer_mapping,
        upcoming_user_details,
        upcoming_user_activity
    )

    report = ChurnReport(
        summary=summary,
        churned_users=humans,
        bot_users=bots,
        upcoming_cancellations=upcoming_cancellations,
        friction_data=friction_data,
        historical_metrics=historical_metrics
    )

    logger.info("Churn report generated successfully")

    return report


def format_report_console(report: ChurnReport, verbose: bool = False) -> str:
    """
    Format the report for console output.
    """
    s = report.summary
    lines = []

    # Header
    lines.append("")
    lines.append("=" * 70)
    lines.append("                    CHURN ANALYSIS REPORT")
    lines.append(f"         Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"              Period: Last {s.period_days} days")
    lines.append("=" * 70)

    # Executive Summary
    lines.append("")
    lines.append("EXECUTIVE SUMMARY")
    lines.append("-" * 70)

    churn_indicator = ""
    if s.churn_rate_change is not None:
        if s.churn_rate_change > 0:
            churn_indicator = f" (UP {s.churn_rate_change:+.1f}%)"
        elif s.churn_rate_change < 0:
            churn_indicator = f" (DOWN {s.churn_rate_change:+.1f}%)"

    lines.append(f"  Churn Rate:           {s.current_churn_rate:.1f}%{churn_indicator}" if s.current_churn_rate else "  Churn Rate:           N/A")
    lines.append(f"  MRR Lost:             ${s.mrr_lost_filtered:,.2f}")
    lines.append(f"  Churned Users:        {s.total_churned_filtered} ({s.bots_filtered} bots filtered)")
    lines.append(f"  Avg Subscription:     {s.avg_tenure_days:.1f} days")

    # Tenure Segment Breakdown
    lines.append("")
    lines.append("CHURN BY TENURE SEGMENT")
    lines.append("-" * 70)

    max_count = max((seg['count'] for seg in s.segment_breakdown.values()), default=1)
    for segment in ['<7d', '7-30d', '30+d']:
        data = s.segment_breakdown.get(segment, {'count': 0, 'mrr_lost': 0, 'pct_of_total': 0})
        bar_len = int((data['count'] / max(max_count, 1)) * 30)
        bar = '#' * bar_len

        lines.append(f"  {segment:8} {bar:30} {data['pct_of_total']:5.1f}% ({data['count']:3} users, ${data['mrr_lost']:,.0f} MRR)")

    # Cancellation Reasons
    if s.cancellation_reasons:
        lines.append("")
        lines.append("TOP CANCELLATION REASONS")
        lines.append("-" * 70)

        sorted_reasons = sorted(s.cancellation_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
        for reason, count in sorted_reasons:
            lines.append(f"  {count:3}x  {reason[:55]}")

    # Friction Correlation (Clarity)
    friction = report.friction_data
    if friction.get('page_issues'):
        lines.append("")
        lines.append("UX FRICTION POINTS (Clarity)")
        lines.append("-" * 70)
        lines.append("  Top pages with user frustration signals:")

        for i, page in enumerate(friction['page_issues'][:5], 1):
            path = page.get('path', '/')
            dead = page.get('dead_clicks', 0)
            rage = page.get('rage_clicks', 0)
            quick = page.get('quick_backs', 0)
            lines.append(f"  {i}. {path:30} | {rage:3} rage, {dead:3} dead clicks, {quick:3} quick backs")

        if friction.get('total_sessions'):
            lines.append(f"")
            lines.append(f"  Total sessions: {friction['total_sessions']:,} | "
                        f"Rage clicks: {friction.get('rage_clicks', 0):,} | "
                        f"Dead clicks: {friction.get('dead_clicks', 0):,}")

    # Churned User Details (top 10 or all if verbose)
    lines.append("")
    lines.append("CHURNED USER DETAILS (Filtered)")
    lines.append("-" * 70)

    if report.churned_users:
        if verbose:
            # Detailed view for each user
            for i, user in enumerate(report.churned_users, 1):
                lines.append("")
                lines.append(f"  [{i}] {user.email or 'No email'}")
                lines.append(f"      User ID:      {user.user_id}")
                lines.append(f"      Stripe ID:    {user.stripe_customer_id}")
                lines.append(f"      Plan:         {user.plan_name or 'Unknown'}")
                lines.append(f"      Subscribed:   {user.subscription_start.strftime('%Y-%m-%d') if user.subscription_start else 'N/A'}")
                lines.append(f"      Cancelled:    {user.cancellation_date.strftime('%Y-%m-%d') if user.cancellation_date else 'N/A'}")
                lines.append(f"      Tenure:       {user.tenure_days} days ({user.tenure_segment})")
                lines.append(f"      MRR Lost:     ${user.mrr_lost:.2f}")
                lines.append(f"      Reason:       {user.cancellation_reason or 'Not specified'}")
                lines.append(f"      Generations:  {user.total_generations} total ({user.successful_generations} success, {user.failed_generations} failed)")
                lines.append(f"      Last Active:  {user.last_activity.strftime('%Y-%m-%d %H:%M') if user.last_activity else 'No activity recorded'}")
        else:
            # Compact table view
            lines.append(f"  {'Email':30} | {'Cancelled':10} | {'Tenure':7} | {'MRR':>7} | {'Gens':>4}")
            lines.append("  " + "-" * 70)

            display_users = report.churned_users[:10]
            for user in display_users:
                email = (user.email or 'No email')[:30]
                cancelled = user.cancellation_date.strftime('%Y-%m-%d') if user.cancellation_date else 'N/A'
                tenure = f"{user.tenure_days}d"
                mrr = f"${user.mrr_lost:.0f}"
                gens = str(user.total_generations)

                lines.append(f"  {email:30} | {cancelled:10} | {tenure:7} | {mrr:>7} | {gens:>4}")

            if len(report.churned_users) > 10:
                lines.append(f"  ... and {len(report.churned_users) - 10} more (use --verbose to see all)")
    else:
        lines.append("  No churned users found in this period.")

    # Upcoming Cancellations (Future Churn)
    if report.upcoming_cancellations:
        lines.append("")
        lines.append("UPCOMING CANCELLATIONS (Retention Opportunities)")
        lines.append("-" * 70)

        total_at_risk_mrr = sum(u.current_mrr for u in report.upcoming_cancellations)
        high_priority = [u for u in report.upcoming_cancellations if u.retention_priority == 'high']

        lines.append(f"  Users scheduled to cancel: {len(report.upcoming_cancellations)}")
        lines.append(f"  MRR at risk: ${total_at_risk_mrr:,.2f}")
        lines.append(f"  High-priority retention: {len(high_priority)}")
        lines.append("")

        if verbose:
            # Detailed view
            for i, user in enumerate(report.upcoming_cancellations, 1):
                priority_emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(user.retention_priority, '⚪')
                lines.append(f"  [{i}] {priority_emoji} {user.email or 'No email'}")
                lines.append(f"      Cancels:      {user.cancel_at.strftime('%Y-%m-%d')} ({user.days_until_cancel} days)")
                lines.append(f"      MRR at risk:  ${user.current_mrr:.2f}")
                lines.append(f"      Tenure:       {user.tenure_days} days")
                lines.append(f"      Generations:  {user.total_generations} total")
                lines.append(f"      Priority:     {user.retention_priority.upper()}")
                if user.retention_notes:
                    lines.append(f"      Notes:        {'; '.join(user.retention_notes)}")
                lines.append("")
        else:
            # Compact view - show high priority first
            lines.append(f"  {'Priority':8} | {'Email':25} | {'Cancels':10} | {'MRR':>7} | {'Gens':>4}")
            lines.append("  " + "-" * 62)
            for user in report.upcoming_cancellations[:10]:
                priority_emoji = {'high': '🔴 HIGH', 'medium': '🟡 MED', 'low': '🟢 LOW'}.get(user.retention_priority, '   ')
                email_short = (user.email or 'N/A')[:25]
                cancel_date = user.cancel_at.strftime('%Y-%m-%d')
                lines.append(f"  {priority_emoji:8} | {email_short:25} | {cancel_date:10} | ${user.current_mrr:>5.0f} | {user.total_generations:>4}")

            if len(report.upcoming_cancellations) > 10:
                lines.append(f"  ... and {len(report.upcoming_cancellations) - 10} more (use --verbose to see all)")

    # Bot Detection Summary
    if report.bot_users:
        lines.append("")
        lines.append("BOT DETECTION SUMMARY")
        lines.append("-" * 70)
        lines.append(f"  Accounts flagged as bots: {len(report.bot_users)} ({len(report.bot_users)/(len(report.bot_users)+len(report.churned_users))*100:.1f}% of cancellations)")

        # Group by reason
        reason_counts = {}
        for user in report.bot_users:
            for reason in user.bot_reasons:
                # Simplify reason for grouping
                key = reason.split(':')[0] if ':' in reason else reason
                reason_counts[key] = reason_counts.get(key, 0) + 1

        if reason_counts:
            lines.append("  Detection reasons:")
            for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                lines.append(f"    {count:3}x  {reason}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("")

    return "\n".join(lines)


def format_report_markdown(report: ChurnReport, verbose: bool = False) -> str:
    """
    Format the report as Markdown for saving to file.
    """
    s = report.summary
    lines = []

    # Header
    lines.append("# Churn Analysis Report")
    lines.append("")
    lines.append(f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Period:** Last {s.period_days} days")
    lines.append("")

    # Executive Summary
    lines.append("## Executive Summary")
    lines.append("")

    churn_indicator = ""
    if s.churn_rate_change is not None:
        if s.churn_rate_change > 0:
            churn_indicator = f" (UP {s.churn_rate_change:+.1f}%)"
        elif s.churn_rate_change < 0:
            churn_indicator = f" (DOWN {s.churn_rate_change:+.1f}%)"

    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    churn_val = f"{s.current_churn_rate:.1f}%{churn_indicator}" if s.current_churn_rate else "N/A"
    lines.append(f"| Churn Rate | {churn_val} |")
    lines.append(f"| MRR Lost | ${s.mrr_lost_filtered:,.2f} |")
    lines.append(f"| Churned Users | {s.total_churned_filtered} ({s.bots_filtered} bots filtered) |")
    lines.append(f"| Avg Subscription Length | {s.avg_tenure_days:.1f} days |")
    lines.append("")

    # Tenure Segment Breakdown
    lines.append("## Churn by Tenure Segment")
    lines.append("")
    lines.append("| Segment | Users | % of Total | MRR Lost |")
    lines.append("|---------|-------|------------|----------|")

    for segment in ['<7d', '7-30d', '30+d']:
        data = s.segment_breakdown.get(segment, {'count': 0, 'mrr_lost': 0, 'pct_of_total': 0})
        lines.append(f"| {segment} | {data['count']} | {data['pct_of_total']:.1f}% | ${data['mrr_lost']:,.0f} |")

    lines.append("")

    # Cancellation Reasons
    if s.cancellation_reasons:
        lines.append("## Top Cancellation Reasons")
        lines.append("")
        sorted_reasons = sorted(s.cancellation_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
        for reason, count in sorted_reasons:
            lines.append(f"- **{count}x** {reason}")
        lines.append("")

    # Friction Points
    friction = report.friction_data
    if friction.get('page_issues'):
        lines.append("## UX Friction Points (Clarity)")
        lines.append("")
        lines.append("Top pages with user frustration signals:")
        lines.append("")
        lines.append("| Page | Rage Clicks | Dead Clicks | Quick Backs |")
        lines.append("|------|-------------|-------------|-------------|")

        for page in friction['page_issues'][:10]:
            path = page.get('path', '/')
            lines.append(f"| {path} | {page.get('rage_clicks', 0)} | {page.get('dead_clicks', 0)} | {page.get('quick_backs', 0)} |")

        lines.append("")
        if friction.get('total_sessions'):
            lines.append(f"*Total sessions: {friction['total_sessions']:,} | "
                        f"Total rage clicks: {friction.get('rage_clicks', 0):,} | "
                        f"Total dead clicks: {friction.get('dead_clicks', 0):,}*")
            lines.append("")

    # Churned User Details
    lines.append("## Churned User Details")
    lines.append("")

    if report.churned_users:
        if verbose:
            # Detailed cards for each user
            for i, user in enumerate(report.churned_users, 1):
                lines.append(f"### {i}. {user.email or 'No email on file'}")
                lines.append("")
                lines.append("| Field | Value |")
                lines.append("|-------|-------|")
                lines.append(f"| User ID | `{user.user_id}` |")
                lines.append(f"| Stripe Customer | `{user.stripe_customer_id}` |")
                lines.append(f"| Plan | {user.plan_name or 'Unknown'} |")
                lines.append(f"| Subscribed | {user.subscription_start.strftime('%Y-%m-%d') if user.subscription_start else 'N/A'} |")
                lines.append(f"| Cancelled | {user.cancellation_date.strftime('%Y-%m-%d') if user.cancellation_date else 'N/A'} |")
                lines.append(f"| Tenure | {user.tenure_days} days ({user.tenure_segment}) |")
                lines.append(f"| MRR Lost | ${user.mrr_lost:.2f} |")
                lines.append(f"| Cancellation Reason | {user.cancellation_reason or 'Not specified'} |")
                lines.append(f"| Total Generations | {user.total_generations} ({user.successful_generations} success, {user.failed_generations} failed) |")
                lines.append(f"| Last Activity | {user.last_activity.strftime('%Y-%m-%d %H:%M UTC') if user.last_activity else 'No activity recorded'} |")
                lines.append("")
        else:
            # Compact table
            lines.append("| Email | Cancelled | Tenure | MRR | Generations | Last Activity |")
            lines.append("|-------|-----------|--------|-----|-------------|---------------|")

            display_users = report.churned_users[:15]
            for user in display_users:
                email = user.email or 'No email'
                cancelled = user.cancellation_date.strftime('%Y-%m-%d') if user.cancellation_date else 'N/A'
                tenure = f"{user.tenure_days}d"
                mrr = f"${user.mrr_lost:.0f}"
                gens = str(user.total_generations)
                last_act = user.last_activity.strftime('%Y-%m-%d') if user.last_activity else 'None'

                lines.append(f"| {email} | {cancelled} | {tenure} | {mrr} | {gens} | {last_act} |")

            if len(report.churned_users) > 15:
                lines.append(f"\n*...and {len(report.churned_users) - 15} more users*")
    else:
        lines.append("*No churned users found in this period.*")

    lines.append("")

    # Upcoming Cancellations (Future Churn)
    if report.upcoming_cancellations:
        lines.append("## Upcoming Cancellations (Retention Opportunities)")
        lines.append("")

        total_at_risk_mrr = sum(u.current_mrr for u in report.upcoming_cancellations)
        high_priority = [u for u in report.upcoming_cancellations if u.retention_priority == 'high']
        medium_priority = [u for u in report.upcoming_cancellations if u.retention_priority == 'medium']

        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Users scheduled to cancel | {len(report.upcoming_cancellations)} |")
        lines.append(f"| MRR at risk | ${total_at_risk_mrr:,.2f} |")
        lines.append(f"| High priority | {len(high_priority)} |")
        lines.append(f"| Medium priority | {len(medium_priority)} |")
        lines.append("")

        if verbose:
            # Detailed cards for each user
            for i, user in enumerate(report.upcoming_cancellations, 1):
                priority_badge = {'high': '🔴 HIGH', 'medium': '🟡 MEDIUM', 'low': '🟢 LOW'}.get(user.retention_priority, '')
                lines.append(f"### {i}. {user.email or 'No email'} - {priority_badge}")
                lines.append("")
                lines.append("| Field | Value |")
                lines.append("|-------|-------|")
                lines.append(f"| User ID | `{user.user_id}` |")
                lines.append(f"| Cancels On | {user.cancel_at.strftime('%Y-%m-%d')} ({user.days_until_cancel} days) |")
                lines.append(f"| MRR at Risk | ${user.current_mrr:.2f} |")
                lines.append(f"| Tenure | {user.tenure_days} days |")
                lines.append(f"| Plan | {user.plan_name or 'Unknown'} |")
                lines.append(f"| Generations | {user.total_generations} ({user.successful_generations} success) |")
                lines.append(f"| Last Activity | {user.last_activity.strftime('%Y-%m-%d') if user.last_activity else 'Never'} |")
                lines.append("")
                if user.retention_notes:
                    lines.append("**Retention Notes:**")
                    for note in user.retention_notes:
                        lines.append(f"- {note}")
                    lines.append("")
        else:
            # Compact table
            lines.append("| Priority | Email | Cancels | MRR | Gens |")
            lines.append("|----------|-------|---------|-----|------|")
            for user in report.upcoming_cancellations[:15]:
                priority_badge = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(user.retention_priority, '')
                email_short = (user.email or 'N/A')[:30]
                lines.append(f"| {priority_badge} {user.retention_priority.upper()} | {email_short} | {user.cancel_at.strftime('%Y-%m-%d')} | ${user.current_mrr:.0f} | {user.total_generations} |")

            if len(report.upcoming_cancellations) > 15:
                lines.append(f"\n*...and {len(report.upcoming_cancellations) - 15} more*")

        lines.append("")

    # Bot Detection Summary
    if report.bot_users:
        lines.append("## Bot Detection Summary")
        lines.append("")
        pct = len(report.bot_users) / (len(report.bot_users) + len(report.churned_users)) * 100
        lines.append(f"**{len(report.bot_users)} accounts** flagged as bots ({pct:.1f}% of cancellations)")
        lines.append("")

        # Group by reason
        reason_counts = {}
        for user in report.bot_users:
            for reason in user.bot_reasons:
                key = reason.split(':')[0] if ':' in reason else reason
                reason_counts[key] = reason_counts.get(key, 0) + 1

        if reason_counts:
            lines.append("Detection reasons:")
            for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                lines.append(f"- {count}x {reason}")

        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Report generated by GenTube Optimization Machine*")
    lines.append("")

    return "\n".join(lines)


def save_report(report: ChurnReport, verbose: bool = False) -> str:
    """
    Save the report to the cache directory.

    Returns:
        Path to the saved file
    """
    # Ensure cache/reports directory exists
    reports_dir = os.path.join('cache', 'reports')
    os.makedirs(reports_dir, exist_ok=True)

    filepath = os.path.join(reports_dir, 'churn_analysis.md')

    markdown_content = format_report_markdown(report, verbose)

    with open(filepath, 'w') as f:
        f.write(markdown_content)

    logger.info(f"Report saved to {filepath}")

    return filepath


def run_churn_report(days: int = 30, verbose: bool = False) -> int:
    """
    Main entry point for CLI command.

    Returns:
        Exit code (0 = success)
    """
    try:
        # Generate the report
        report = generate_churn_report(days=days, verbose=verbose)

        # Print to console
        console_output = format_report_console(report, verbose)
        print(console_output)

        # Save to file
        filepath = save_report(report, verbose)
        print(f"Report saved to: {filepath}")

        return 0

    except Exception as e:
        logger.error(f"Churn report failed: {e}", exc_info=True)
        print(f"\nError generating churn report: {e}\n")
        return 1


# Singleton instance for module use
_churn_report_instance = None


def get_latest_churn_report() -> Optional[ChurnReport]:
    """Get the most recently generated report (for use by other modules)."""
    global _churn_report_instance
    return _churn_report_instance


if __name__ == '__main__':
    # Allow running directly for testing
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    sys.exit(run_churn_report(days=days, verbose=verbose))
