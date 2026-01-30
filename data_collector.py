"""
Data & Feedback Collector
Collects metrics, user feedback, and analyzes data post-deployment
"""

import os
import json
import logging
import time
import base64
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import stripe
import pandas as pd
import requests
from transformers import pipeline
from dotenv import load_dotenv
from supabase import create_client, Client
from db_handler import get_db_handler

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DataCollector:
    """Collects and analyzes metrics and feedback"""

    def __init__(self):
        self.db = get_db_handler()

        # Initialize Stripe
        stripe_key = os.getenv('STRIPE_API_KEY')
        if stripe_key:
            stripe.api_key = stripe_key
            logger.info("Stripe API initialized")
        else:
            logger.warning("STRIPE_API_KEY not set")

        # Initialize sentiment analyzer (using lightweight model)
        try:
            self.sentiment_analyzer = pipeline(
                "sentiment-analysis",
                model="distilbert-base-uncased-finetuned-sst-2-english"
            )
            logger.info("Sentiment analyzer initialized")
        except Exception as e:
            logger.warning(f"Sentiment analyzer initialization failed: {e}")
            self.sentiment_analyzer = None

        self.gentube_api = os.getenv('GENTUBE_API_ENDPOINT')

        # Initialize Supabase auth client (separate instance for user data)
        self.supabase_auth: Optional[Client] = None
        supabase_auth_url = os.getenv('SUPABASE_AUTH_URL')
        supabase_auth_key = os.getenv('SUPABASE_AUTH_KEY')
        if supabase_auth_url and supabase_auth_key:
            try:
                self.supabase_auth = create_client(supabase_auth_url, supabase_auth_key)
                logger.info("Supabase auth client initialized")
            except Exception as e:
                logger.warning(f"Supabase auth client initialization failed: {e}")
        else:
            logger.warning("SUPABASE_AUTH_URL/SUPABASE_AUTH_KEY not set - user metrics unavailable")

        # Initialize Google Cloud Datastore (UserActivity)
        self.datastore_client = None
        self.gcd_excluded_user_ids = set()
        gcd_project_id = os.getenv('GCD_PROJECT_ID')
        gcd_client_email = os.getenv('GCD_CLIENT_EMAIL')
        gcd_private_key = os.getenv('GCD_PRIVATE_KEY')
        if gcd_project_id and gcd_client_email and gcd_private_key:
            try:
                from google.cloud import datastore
                from google.oauth2 import service_account
                creds_info = {
                    'type': os.getenv('GCD_TYPE', 'service_account'),
                    'project_id': gcd_project_id,
                    'private_key_id': os.getenv('GCD_PRIVATE_KEY_ID', ''),
                    'private_key': gcd_private_key.replace('\\n', '\n'),
                    'client_email': gcd_client_email,
                    'client_id': os.getenv('GCD_CLIENT_ID', ''),
                    'auth_uri': os.getenv('GCD_AUTH_URI', 'https://accounts.google.com/o/oauth2/auth'),
                    'token_uri': os.getenv('GCD_TOKEN_URI', 'https://oauth2.googleapis.com/token'),
                    'auth_provider_x509_cert_url': os.getenv('GCD_AUTH_PROVIDER_X509_CERT_URL', ''),
                    'client_x509_cert_url': os.getenv('GCD_CLIENT_X509_CERT_URL', ''),
                }
                credentials = service_account.Credentials.from_service_account_info(creds_info)
                gcd_namespace = os.getenv('GCD_NAMESPACE', 'GenTube')
                self.datastore_client = datastore.Client(
                    project=gcd_project_id, credentials=credentials, namespace=gcd_namespace
                )
                logger.info("Google Cloud Datastore client initialized")
            except Exception as e:
                logger.warning(f"Datastore client initialization failed: {e}")
        else:
            logger.warning("GCD_PROJECT_ID/GCD_CLIENT_EMAIL/GCD_PRIVATE_KEY not set - generation metrics unavailable")

        excluded = os.getenv('GCD_EXCLUDED_USER_IDS', '')
        if excluded:
            self.gcd_excluded_user_ids = {uid.strip() for uid in excluded.split(',') if uid.strip()}

        # Initialize Google Analytics 4
        self.ga4_client = None
        self.ga4_property_id = os.getenv('GA4_PROPERTY_ID')
        ga4_creds_path = os.getenv('GA4_CREDENTIALS_PATH')
        ga4_creds_json = os.getenv('GA4_CREDENTIALS_JSON')

        if self.ga4_property_id and (ga4_creds_path or ga4_creds_json):
            try:
                from google.analytics.data_v1beta import BetaAnalyticsDataClient
                from google.oauth2 import service_account

                if ga4_creds_path and os.path.exists(ga4_creds_path):
                    credentials = service_account.Credentials.from_service_account_file(
                        ga4_creds_path,
                        scopes=['https://www.googleapis.com/auth/analytics.readonly']
                    )
                elif ga4_creds_json:
                    creds_dict = json.loads(base64.b64decode(ga4_creds_json))
                    credentials = service_account.Credentials.from_service_account_info(
                        creds_dict,
                        scopes=['https://www.googleapis.com/auth/analytics.readonly']
                    )
                else:
                    raise ValueError("No valid GA4 credentials found")

                self.ga4_client = BetaAnalyticsDataClient(credentials=credentials)
                logger.info("Google Analytics 4 API initialized")
            except Exception as e:
                logger.warning(f"GA4 initialization failed: {e}")
                self.ga4_client = None
        else:
            logger.warning("GA4_PROPERTY_ID or credentials not set")

        # Initialize CrUX API
        self.crux_api_key = os.getenv('CRUX_API_KEY')
        self.gentube_origin = os.getenv('GENTUBE_ORIGIN', os.getenv('GENTUBE_APP_URL'))
        if self.crux_api_key:
            logger.info("CrUX API initialized")
        else:
            logger.warning("CRUX_API_KEY not set")

        logger.info("Data collector initialized")

    def collect_metrics(self, strategy_id: Optional[int] = None,
                        cycle_id: Optional[str] = None,
                        wait_hours: int = 6) -> Dict[str, Any]:
        """
        Collect comprehensive metrics

        Args:
            strategy_id: Strategy ID (if collecting post-deployment)
            cycle_id: Cycle ID
            wait_hours: Hours to wait after deployment before collecting

        Returns:
            Metrics dictionary
        """
        logger.info(f"Collecting metrics (waiting {wait_hours} hours)...")

        # Wait for data to accumulate
        if wait_hours > 0:
            time.sleep(wait_hours * 3600)

        metrics = {}

        try:
            # Collect from various sources
            metrics['stripe'] = self._collect_stripe_metrics()
            metrics['app_usage'] = self._collect_app_usage_metrics()
            metrics['feedback'] = self._collect_feedback_metrics()

            # Collect analytics and performance metrics
            metrics['google_analytics'] = self._collect_ga4_metrics()
            metrics['web_vitals'] = self._collect_crux_metrics()

            # Calculate derived metrics (includes new analytics-based calculations)
            metrics['calculated'] = self._calculate_derived_metrics(metrics)

            # Create summary
            summary = {
                'strategy_id': strategy_id,
                'cycle_id': cycle_id,
                'timestamp': datetime.utcnow().isoformat(),
                # Existing fields
                'dau': metrics['app_usage'].get('dau'),
                'mau': metrics['app_usage'].get('mau'),
                'arpu': metrics['calculated'].get('arpu'),
                'revenue': metrics['stripe'].get('revenue'),
                'churn_rate': metrics['calculated'].get('churn_rate'),
                'credit_usage': metrics['app_usage'].get('total_credits_used'),
                'gen_success_rate': metrics['app_usage'].get('gen_success_rate'),
                'avg_session_iterations': metrics['app_usage'].get('avg_session_iterations'),
                # GA4 summary fields
                'ga_sessions': metrics['google_analytics'].get('sessions'),
                'ga_users': metrics['google_analytics'].get('total_users'),
                'bounce_rate': metrics['google_analytics'].get('bounce_rate'),
                'avg_session_duration': metrics['google_analytics'].get('avg_session_duration_seconds'),
                # CrUX summary fields
                'lcp_p75': metrics['web_vitals'].get('lcp', {}).get('p75_ms'),
                'cls_p75': metrics['web_vitals'].get('cls', {}).get('p75'),
                'inp_p75': metrics['web_vitals'].get('inp', {}).get('p75_ms'),
                'core_web_vitals_passing': metrics['web_vitals'].get('core_web_vitals_passing'),
                # Derived analytics metrics
                'engagement_rate': metrics['calculated'].get('engagement_rate'),
                'performance_score': metrics['calculated'].get('performance_score'),
                'period_start': (datetime.utcnow() - timedelta(hours=wait_hours)).isoformat(),
                'period_end': datetime.utcnow().isoformat(),
                'kpi_data': metrics
            }

            # Log to database
            self.db.log_metrics(summary)

            logger.info("Metrics collected successfully")
            return summary

        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
            raise

    def _collect_stripe_metrics(self) -> Dict[str, Any]:
        """Collect revenue and payment metrics from Stripe (30-day + month-over-month)"""
        logger.info("Collecting Stripe metrics...")

        if not stripe.api_key:
            return {'error': 'Stripe not configured'}

        try:
            now = datetime.utcnow()
            thirty_days_ago = int((now - timedelta(days=30)).timestamp())
            sixty_days_ago = int((now - timedelta(days=60)).timestamp())

            # Fetch all charges for the last 60 days for MoM comparison
            all_charges = []
            has_more = True
            starting_after = None
            while has_more:
                params = {'created': {'gte': sixty_days_ago}, 'limit': 100}
                if starting_after:
                    params['starting_after'] = starting_after
                batch = stripe.Charge.list(**params)
                all_charges.extend(batch.data)
                has_more = batch.has_more
                if batch.data:
                    starting_after = batch.data[-1].id

            # Split into current month (last 30 days) and previous month (30-60 days ago)
            current_charges = [c for c in all_charges if c.created >= thirty_days_ago]
            previous_charges = [c for c in all_charges if c.created < thirty_days_ago]

            current_revenue = sum(c.amount / 100 for c in current_charges if c.paid)
            previous_revenue = sum(c.amount / 100 for c in previous_charges if c.paid)
            current_successful = len([c for c in current_charges if c.paid])
            previous_successful = len([c for c in previous_charges if c.paid])
            current_failed = len([c for c in current_charges if not c.paid])

            # MoM growth
            if previous_revenue > 0:
                revenue_mom_pct = round((current_revenue - previous_revenue) / previous_revenue * 100, 1)
            else:
                revenue_mom_pct = None

            # Get active subscriptions for MRR calculation
            subscriptions = stripe.Subscription.list(status='active', limit=100)
            active_subs = subscriptions.data
            mrr = sum(
                (s.plan.amount / 100) if s.plan.interval == 'month'
                else (s.plan.amount / 100 / 12) if s.plan.interval == 'year'
                else 0
                for s in active_subs if hasattr(s, 'plan') and s.plan
            )

            # Get customer count
            customers = stripe.Customer.list(limit=100)
            active_customers = len([c for c in customers.data if not c.get('deleted')])

            return {
                'revenue': current_revenue,
                'revenue_previous_period': previous_revenue,
                'revenue_mom_pct': revenue_mom_pct,
                'mrr': round(mrr, 2),
                'successful_charges': current_successful,
                'successful_charges_previous': previous_successful,
                'failed_charges': current_failed,
                'active_customers': active_customers,
                'active_subscriptions': len(active_subs),
                'period_days': 30
            }

        except Exception as e:
            logger.error(f"Stripe metrics collection failed: {e}")
            return {'error': str(e)}

    def _collect_ga4_metrics(self) -> Dict[str, Any]:
        """Collect user behavior metrics from Google Analytics 4"""
        logger.info("Collecting Google Analytics 4 metrics...")

        if not self.ga4_client or not self.ga4_property_id:
            return {'error': 'Google Analytics 4 not configured'}

        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                FilterExpression,
                Filter,
            )

            # Define date range (last 7 days to match Stripe)
            date_range = DateRange(start_date="7daysAgo", end_date="today")

            # Request 1: Core engagement metrics
            engagement_request = RunReportRequest(
                property=self.ga4_property_id,
                date_ranges=[date_range],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="activeUsers"),
                    Metric(name="screenPageViews"),
                    Metric(name="bounceRate"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="engagedSessions"),
                    Metric(name="userEngagementDuration"),
                ],
            )

            engagement_response = self.ga4_client.run_report(engagement_request)

            # Parse engagement metrics
            engagement_metrics = {}
            if engagement_response.rows:
                row = engagement_response.rows[0]
                metric_names = [m.name for m in engagement_response.metric_headers]
                for i, value in enumerate(row.metric_values):
                    val = value.value
                    engagement_metrics[metric_names[i]] = float(val) if '.' in val else int(val)

            # Request 2: Traffic sources breakdown
            traffic_request = RunReportRequest(
                property=self.ga4_property_id,
                date_ranges=[date_range],
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="conversions"),
                ],
                limit=10,
            )

            traffic_response = self.ga4_client.run_report(traffic_request)

            # Parse traffic sources
            traffic_sources = {}
            for row in traffic_response.rows:
                channel = row.dimension_values[0].value
                traffic_sources[channel] = {
                    'sessions': int(row.metric_values[0].value),
                    'users': int(row.metric_values[1].value),
                    'conversions': int(row.metric_values[2].value),
                }

            # Request 3: Top pages
            pages_request = RunReportRequest(
                property=self.ga4_property_id,
                date_ranges=[date_range],
                dimensions=[Dimension(name="pagePath")],
                metrics=[
                    Metric(name="screenPageViews"),
                    Metric(name="averageSessionDuration"),
                ],
                limit=10,
            )

            pages_response = self.ga4_client.run_report(pages_request)

            # Parse top pages
            top_pages = []
            for row in pages_response.rows:
                top_pages.append({
                    'path': row.dimension_values[0].value,
                    'pageviews': int(row.metric_values[0].value),
                    'avg_duration': float(row.metric_values[1].value),
                })

            # Request 4: Conversion events (key events for Gentube)
            # Note: We'll try to get events, but filter may fail if events don't exist
            conversion_events = {}
            try:
                conversion_request = RunReportRequest(
                    property=self.ga4_property_id,
                    date_ranges=[date_range],
                    dimensions=[Dimension(name="eventName")],
                    metrics=[
                        Metric(name="eventCount"),
                    ],
                    dimension_filter=FilterExpression(
                        filter=Filter(
                            field_name="eventName",
                            in_list_filter=Filter.InListFilter(
                                values=[
                                    "sign_up",
                                    "purchase",
                                    "generate_image",
                                    "generate_video",
                                    "share_creation",
                                    "upgrade_plan",
                                ]
                            )
                        )
                    ),
                )

                conversion_response = self.ga4_client.run_report(conversion_request)

                for row in conversion_response.rows:
                    event_name = row.dimension_values[0].value
                    conversion_events[event_name] = {
                        'count': int(row.metric_values[0].value),
                    }
            except Exception as e:
                logger.warning(f"Could not fetch conversion events: {e}")

            return {
                'sessions': engagement_metrics.get('sessions', 0),
                'total_users': engagement_metrics.get('totalUsers', 0),
                'active_users': engagement_metrics.get('activeUsers', 0),
                'pageviews': engagement_metrics.get('screenPageViews', 0),
                'bounce_rate': round(engagement_metrics.get('bounceRate', 0) * 100, 2),
                'avg_session_duration_seconds': engagement_metrics.get('averageSessionDuration', 0),
                'engaged_sessions': engagement_metrics.get('engagedSessions', 0),
                'total_engagement_duration_seconds': engagement_metrics.get('userEngagementDuration', 0),
                'traffic_sources': traffic_sources,
                'top_pages': top_pages,
                'conversion_events': conversion_events,
                'period_days': 7,
            }

        except Exception as e:
            logger.error(f"GA4 metrics collection failed: {e}")
            return {'error': str(e)}

    def _collect_crux_metrics(self) -> Dict[str, Any]:
        """Collect Core Web Vitals from Chrome UX Report API"""
        logger.info("Collecting CrUX metrics...")

        if not self.crux_api_key:
            return {'error': 'CrUX API not configured'}

        if not self.gentube_origin:
            return {'error': 'GENTUBE_ORIGIN not configured'}

        try:
            crux_url = f"https://chromeuxreport.googleapis.com/v1/records:queryRecord?key={self.crux_api_key}"

            # Normalize origin URL (remove trailing slash)
            origin = self.gentube_origin.rstrip('/')

            # Request origin-level data for mobile (primary)
            # Note: time_to_first_byte is not a valid CrUX metric
            origin_payload = {
                "origin": origin,
                "formFactor": "PHONE",
                "metrics": [
                    "largest_contentful_paint",
                    "cumulative_layout_shift",
                    "interaction_to_next_paint",
                    "first_contentful_paint",
                ]
            }

            response = requests.post(crux_url, json=origin_payload, timeout=30)

            if response.status_code == 400:
                error_msg = response.text
                logger.warning(f"CrUX API bad request for origin '{origin}': {error_msg}")
                return {
                    'error': 'bad_request',
                    'message': f'Invalid origin or request format: {error_msg}'
                }

            if response.status_code == 404:
                logger.warning("Origin not found in CrUX dataset - insufficient traffic data")
                return {
                    'error': 'insufficient_traffic',
                    'message': 'Origin does not have enough traffic for CrUX data'
                }

            response.raise_for_status()
            data = response.json()

            # Parse metrics from CrUX response
            metrics = {}
            record = data.get('record', {})
            crux_metrics = record.get('metrics', {})

            # Helper to extract p75 value
            def get_p75(metric_data: Dict) -> Optional[float]:
                percentiles = metric_data.get('percentiles', {})
                return percentiles.get('p75')

            # Helper to get distribution percentages
            def get_distribution(metric_data: Dict) -> Dict[str, float]:
                histogram = metric_data.get('histogram', [])
                result = {'good': 0, 'needs_improvement': 0, 'poor': 0}
                labels = ['good', 'needs_improvement', 'poor']
                for i, bucket in enumerate(histogram[:3]):
                    if i < len(labels):
                        result[labels[i]] = round(bucket.get('density', 0) * 100, 2)
                return result

            # Extract Core Web Vitals
            if 'largest_contentful_paint' in crux_metrics:
                lcp_data = crux_metrics['largest_contentful_paint']
                lcp_p75 = get_p75(lcp_data)
                metrics['lcp'] = {
                    'p75_ms': lcp_p75,
                    'distribution': get_distribution(lcp_data),
                    'status': 'good' if lcp_p75 and lcp_p75 <= 2500 else 'needs_improvement' if lcp_p75 and lcp_p75 <= 4000 else 'poor'
                }

            if 'cumulative_layout_shift' in crux_metrics:
                cls_data = crux_metrics['cumulative_layout_shift']
                cls_p75 = get_p75(cls_data)
                metrics['cls'] = {
                    'p75': cls_p75,
                    'distribution': get_distribution(cls_data),
                    'status': 'good' if cls_p75 and cls_p75 <= 0.1 else 'needs_improvement' if cls_p75 and cls_p75 <= 0.25 else 'poor'
                }

            if 'interaction_to_next_paint' in crux_metrics:
                inp_data = crux_metrics['interaction_to_next_paint']
                inp_p75 = get_p75(inp_data)
                metrics['inp'] = {
                    'p75_ms': inp_p75,
                    'distribution': get_distribution(inp_data),
                    'status': 'good' if inp_p75 and inp_p75 <= 200 else 'needs_improvement' if inp_p75 and inp_p75 <= 500 else 'poor'
                }

            if 'first_contentful_paint' in crux_metrics:
                fcp_data = crux_metrics['first_contentful_paint']
                metrics['fcp'] = {
                    'p75_ms': get_p75(fcp_data),
                    'distribution': get_distribution(fcp_data),
                }

            if 'time_to_first_byte' in crux_metrics:
                ttfb_data = crux_metrics['time_to_first_byte']
                metrics['ttfb'] = {
                    'p75_ms': get_p75(ttfb_data),
                    'distribution': get_distribution(ttfb_data),
                }

            # Also collect desktop metrics for comparison
            desktop_payload = {
                "origin": origin,
                "formFactor": "DESKTOP",
                "metrics": ["largest_contentful_paint", "cumulative_layout_shift", "interaction_to_next_paint"]
            }

            desktop_response = requests.post(crux_url, json=desktop_payload, timeout=30)
            if desktop_response.status_code == 200:
                desktop_data = desktop_response.json()
                desktop_record = desktop_data.get('record', {})
                desktop_metrics = desktop_record.get('metrics', {})

                metrics['desktop'] = {}
                for metric_name in ['largest_contentful_paint', 'cumulative_layout_shift', 'interaction_to_next_paint']:
                    if metric_name in desktop_metrics:
                        short_name = metric_name.replace('_', '')
                        metrics['desktop'][short_name] = {
                            'p75': get_p75(desktop_metrics[metric_name])
                        }

            # Calculate overall Core Web Vitals score
            cwv_passing = all([
                metrics.get('lcp', {}).get('status') == 'good',
                metrics.get('cls', {}).get('status') == 'good',
                metrics.get('inp', {}).get('status') == 'good',
            ])

            metrics['core_web_vitals_passing'] = cwv_passing
            metrics['form_factor'] = 'PHONE'
            metrics['collection_period'] = '28_days'

            return metrics

        except requests.exceptions.RequestException as e:
            logger.error(f"CrUX API request failed: {e}")
            return {'error': str(e)}
        except Exception as e:
            logger.error(f"CrUX metrics collection failed: {e}")
            return {'error': str(e)}

    def _collect_datastore_activity(self, days: int = 30) -> Dict[str, Any]:
        """Query Google Cloud Datastore for UserActivity generation metrics"""
        SUCCESS_TYPES = {'img', 'vid', 'video', 'image', 'audio', 'music', 'faceswap'}
        FAILURE_TYPES = {'err', 'image-failed'}
        EXCLUDED_TYPES = {'que', 'upl'}
        # Normalize alternate names for studio usage grouping
        NORMALIZE_MAP = {'image': 'img', 'video': 'vid'}

        result = {
            'total_generations': 0,
            'successful_generations': 0,
            'failed_generations': 0,
            'gen_success_rate': 0.0,
            'total_credits_used': 0,
            'studios_usage': {},
            'unique_generating_users': 0,
        }

        if not self.datastore_client:
            return result

        try:
            from google.cloud import datastore as ds

            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            query = self.datastore_client.query(kind='UserActivity')
            query.add_filter('DateTime', '>=', cutoff)

            successful = 0
            failed = 0
            credits_used = 0
            studios = {}
            user_ids = set()

            for entity in query.fetch():
                user_id = entity.get('UserId')
                # Handle list-type UserId (take first element)
                if isinstance(user_id, list):
                    user_id = user_id[0] if user_id else None
                if user_id and str(user_id) in self.gcd_excluded_user_ids:
                    continue

                asset_type = entity.get('AssetType', '')
                if asset_type in EXCLUDED_TYPES:
                    continue

                if user_id:
                    user_ids.add(str(user_id))

                if asset_type in SUCCESS_TYPES:
                    successful += 1
                elif asset_type in FAILURE_TYPES:
                    failed += 1
                else:
                    continue  # Unknown type, skip

                # Track studio usage with normalized names
                normalized = NORMALIZE_MAP.get(asset_type, asset_type)
                studios[normalized] = studios.get(normalized, 0) + 1

                # Calculate credits consumed
                prev = entity.get('CountedAssetPreviousState', 0) or 0
                curr = entity.get('CountedAssetState', 0) or 0
                diff = prev - curr
                if diff > 0:
                    credits_used += diff

            total = successful + failed
            result['total_generations'] = total
            result['successful_generations'] = successful
            result['failed_generations'] = failed
            result['gen_success_rate'] = round((successful / total) * 100, 2) if total > 0 else 0.0
            result['total_credits_used'] = credits_used
            result['studios_usage'] = studios
            result['unique_generating_users'] = len(user_ids)

            logger.info(f"Datastore activity collected: {total} generations "
                        f"({successful} success, {failed} failed), "
                        f"{credits_used} credits used, {len(user_ids)} unique users")

        except Exception as e:
            logger.error(f"Datastore activity collection failed: {e}")
            result['error'] = str(e)

        return result

    def _collect_app_usage_metrics(self) -> Dict[str, Any]:
        """Collect app usage metrics from Supabase auth database and Datastore"""
        logger.info("Collecting app usage metrics...")

        metrics = {
            'dau': 0,
            'mau': 0,
            'total_users': 0,
            'new_signups_30d': 0,
            'new_signups_7d': 0,
            'unique_users_today': 0,
            'unique_users_month': 0,
            'total_generations': 0,
            'successful_generations': 0,
            'failed_generations': 0,
            'gen_success_rate': 0.0,
            'total_credits_used': 0,
            'avg_session_iterations': 0.0,
            'avg_generation_time': 0.0,
            'studios_usage': {},
        }

        # Collect generation activity from Datastore
        activity = self._collect_datastore_activity(days=30)
        metrics.update({
            'total_generations': activity['total_generations'],
            'successful_generations': activity['successful_generations'],
            'failed_generations': activity['failed_generations'],
            'gen_success_rate': activity['gen_success_rate'],
            'total_credits_used': activity['total_credits_used'],
            'studios_usage': activity['studios_usage'],
            'unique_generating_users': activity.get('unique_generating_users', 0),
        })

        if not self.supabase_auth:
            logger.warning("Supabase auth client not available - skipping user metrics")
            return metrics

        now = datetime.utcnow()
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        # Each RPC call is wrapped individually so one failure doesn't block the rest
        # Total registered users
        try:
            total_resp = self.supabase_auth.rpc('get_user_count', {}).execute()
            if total_resp.data is not None:
                metrics['total_users'] = total_resp.data
        except Exception as e:
            logger.warning(f"get_user_count RPC failed (create the SQL function?): {e}")
            # Fallback: count via public users table
            try:
                total_resp = self.supabase_auth.from_('users').select('id', count='exact').execute()
                metrics['total_users'] = total_resp.count or 0
            except Exception:
                pass

        # MAU: users who signed in within the last 30 days
        try:
            mau_resp = self.supabase_auth.rpc('get_active_users_since', {
                'since_date': thirty_days_ago
            }).execute()
            if mau_resp.data is not None:
                metrics['mau'] = mau_resp.data
                metrics['unique_users_month'] = mau_resp.data
        except Exception as e:
            logger.warning(f"get_active_users_since RPC failed: {e}")

        # DAU: users who signed in today
        try:
            dau_resp = self.supabase_auth.rpc('get_active_users_since', {
                'since_date': today_start
            }).execute()
            if dau_resp.data is not None:
                metrics['dau'] = dau_resp.data
                metrics['unique_users_today'] = dau_resp.data
        except Exception as e:
            logger.warning(f"get_active_users_since (DAU) RPC failed: {e}")

        # New signups in the last 30 days
        try:
            signups_30d_resp = self.supabase_auth.rpc('get_signups_since', {
                'since_date': thirty_days_ago
            }).execute()
            if signups_30d_resp.data is not None:
                metrics['new_signups_30d'] = signups_30d_resp.data
        except Exception as e:
            logger.warning(f"get_signups_since (30d) RPC failed: {e}")

        # New signups in the last 7 days
        try:
            signups_7d_resp = self.supabase_auth.rpc('get_signups_since', {
                'since_date': seven_days_ago
            }).execute()
            if signups_7d_resp.data is not None:
                metrics['new_signups_7d'] = signups_7d_resp.data
        except Exception as e:
            logger.warning(f"get_signups_since (7d) RPC failed: {e}")

        logger.info(f"User metrics collected: {metrics['total_users']} total, "
                    f"{metrics['mau']} MAU, {metrics['dau']} DAU, "
                    f"{metrics['new_signups_30d']} new signups (30d)")

        return metrics

    def _collect_feedback_metrics(self) -> Dict[str, Any]:
        """Collect and analyze user feedback"""
        logger.info("Collecting feedback metrics...")

        try:
            # Get recent feedback from database
            feedback = self.db.get_recent_feedback(days=7, limit=100)

            if not feedback:
                return {
                    'total_feedback': 0,
                    'avg_rating': 0,
                    'sentiment_breakdown': {}
                }

            # Analyze sentiments if not already done
            for fb in feedback:
                if not fb.get('sentiment') and self.sentiment_analyzer:
                    try:
                        text = fb['text']
                        result = self.sentiment_analyzer(text[:512])[0]  # Limit text length
                        sentiment = 'positive' if result['label'] == 'POSITIVE' else 'negative'

                        # Update in database
                        # self.db.update_feedback_sentiment(fb['id'], sentiment)
                    except Exception as e:
                        logger.warning(f"Sentiment analysis failed for feedback {fb['id']}: {e}")

            # Calculate metrics
            total = len(feedback)
            ratings = [fb.get('rating', 0) for fb in feedback if fb.get('rating')]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0

            sentiments = [fb.get('sentiment') for fb in feedback if fb.get('sentiment')]
            sentiment_breakdown = {
                'positive': sentiments.count('positive'),
                'negative': sentiments.count('negative'),
                'neutral': sentiments.count('neutral')
            }

            # Categorize common issues
            issues = self._categorize_feedback(feedback)

            return {
                'total_feedback': total,
                'avg_rating': round(avg_rating, 2),
                'sentiment_breakdown': sentiment_breakdown,
                'common_issues': issues
            }

        except Exception as e:
            logger.error(f"Feedback metrics collection failed: {e}")
            return {'error': str(e)}

    def _categorize_feedback(self, feedback: List[Dict]) -> Dict[str, int]:
        """Categorize feedback into common issues"""
        categories = {
            'generation_quality': 0,
            'performance': 0,
            'ui_ux': 0,
            'pricing': 0,
            'features': 0,
            'bugs': 0,
            'other': 0
        }

        keywords = {
            'generation_quality': ['quality', 'blurry', 'distorted', 'prompt', 'style', 'anime', 'realistic'],
            'performance': ['slow', 'lag', 'loading', 'timeout', 'crash', 'freeze'],
            'ui_ux': ['confusing', 'interface', 'button', 'layout', 'navigation', 'design'],
            'pricing': ['expensive', 'price', 'cost', 'credits', 'subscription', 'payment'],
            'features': ['feature', 'missing', 'wish', 'add', 'need', 'share', 'gallery'],
            'bugs': ['bug', 'error', 'broken', 'not working', 'issue', 'problem']
        }

        for fb in feedback:
            text = fb.get('text', '').lower()
            categorized = False

            for category, words in keywords.items():
                if any(word in text for word in words):
                    categories[category] += 1
                    categorized = True
                    break

            if not categorized:
                categories['other'] += 1

        return categories

    def _calculate_derived_metrics(self, metrics: Dict) -> Dict[str, Any]:
        """Calculate derived metrics from collected data"""
        logger.info("Calculating derived metrics...")

        derived = {}

        try:
            stripe_data = metrics.get('stripe', {})
            usage_data = metrics.get('app_usage', {})
            ga_data = metrics.get('google_analytics', {})
            crux_data = metrics.get('web_vitals', {})

            # ARPU (Average Revenue Per User)
            revenue = stripe_data.get('revenue', 0)
            mau = usage_data.get('mau', 0)
            derived['arpu'] = round(revenue / mau, 2) if mau > 0 else 0

            # Churn rate (simplified - would need more historical data)
            # For MVP, we'll estimate based on active subscriptions vs total customers
            active_subs = stripe_data.get('active_subscriptions', 0)
            total_customers = stripe_data.get('active_customers', 1)
            derived['churn_rate'] = round((1 - (active_subs / total_customers)) * 100, 2) if total_customers > 0 else 0

            # DAU/MAU ratio (engagement metric)
            dau = usage_data.get('dau', 0)
            mau = usage_data.get('mau', 1)
            derived['dau_mau_ratio'] = round(dau / mau, 2) if mau > 0 else 0

            # Revenue per generation
            total_gens = usage_data.get('total_generations', 1)
            derived['revenue_per_generation'] = round(revenue / total_gens, 4) if total_gens > 0 else 0

            # Payment success rate
            successful = stripe_data.get('successful_charges', 0)
            total_charges = successful + stripe_data.get('failed_charges', 0)
            derived['payment_success_rate'] = round((successful / total_charges) * 100, 2) if total_charges > 0 else 0

            # Google Analytics derived metrics
            if ga_data and 'error' not in ga_data:
                sessions = ga_data.get('sessions', 0)
                engaged = ga_data.get('engaged_sessions', 0)
                derived['engagement_rate'] = round((engaged / sessions) * 100, 2) if sessions > 0 else 0

                # Pages per session
                pageviews = ga_data.get('pageviews', 0)
                derived['pages_per_session'] = round(pageviews / sessions, 2) if sessions > 0 else 0

                # Conversion metrics from GA events
                conversion_events = ga_data.get('conversion_events', {})
                signups = conversion_events.get('sign_up', {}).get('count', 0)
                purchases = conversion_events.get('purchase', {}).get('count', 0)
                users = ga_data.get('total_users', 1)

                derived['signup_rate'] = round((signups / users) * 100, 2) if users > 0 else 0
                derived['purchase_conversion_rate'] = round((purchases / users) * 100, 2) if users > 0 else 0

                # Traffic quality score (weighted by conversion)
                traffic_sources = ga_data.get('traffic_sources', {})
                if traffic_sources:
                    total_conversions = sum(s.get('conversions', 0) for s in traffic_sources.values())
                    total_sessions = sum(s.get('sessions', 0) for s in traffic_sources.values())
                    derived['traffic_conversion_rate'] = round((total_conversions / total_sessions) * 100, 2) if total_sessions > 0 else 0

            # Performance derived metrics (CrUX)
            if crux_data and 'error' not in crux_data:
                # Performance score (simplified Google-style scoring)
                lcp_status = crux_data.get('lcp', {}).get('status')
                cls_status = crux_data.get('cls', {}).get('status')
                inp_status = crux_data.get('inp', {}).get('status')

                lcp_score = 100 if lcp_status == 'good' else 50 if lcp_status == 'needs_improvement' else 0
                cls_score = 100 if cls_status == 'good' else 50 if cls_status == 'needs_improvement' else 0
                inp_score = 100 if inp_status == 'good' else 50 if inp_status == 'needs_improvement' else 0

                # Weighted average (LCP and INP are more impactful for user experience)
                derived['performance_score'] = round((lcp_score * 0.4 + cls_score * 0.2 + inp_score * 0.4), 0)

                # Good experience percentage (average of "good" distributions)
                lcp_good = crux_data.get('lcp', {}).get('distribution', {}).get('good', 0)
                cls_good = crux_data.get('cls', {}).get('distribution', {}).get('good', 0)
                inp_good = crux_data.get('inp', {}).get('distribution', {}).get('good', 0)
                derived['good_experience_pct'] = round((lcp_good + cls_good + inp_good) / 3, 2)

            # Cross-source insights: Do poor web vitals correlate with high bounce?
            if ga_data and crux_data and 'error' not in ga_data and 'error' not in crux_data:
                bounce_rate = ga_data.get('bounce_rate', 0)
                lcp_p75 = crux_data.get('lcp', {}).get('p75_ms', 0)
                if bounce_rate > 50 and lcp_p75 and lcp_p75 > 2500:
                    derived['performance_bounce_correlation'] = 'high'
                elif bounce_rate > 40 and lcp_p75 and lcp_p75 > 2000:
                    derived['performance_bounce_correlation'] = 'medium'
                else:
                    derived['performance_bounce_correlation'] = 'low'

        except Exception as e:
            logger.error(f"Error calculating derived metrics: {e}")

        return derived

    def analyze_feedback(self, texts: List[str]) -> Dict[str, Any]:
        """
        Analyze a list of feedback texts for sentiment and themes

        Args:
            texts: List of feedback text

        Returns:
            Analysis results
        """
        logger.info(f"Analyzing {len(texts)} feedback texts...")

        if not self.sentiment_analyzer:
            return {'error': 'Sentiment analyzer not available'}

        try:
            sentiments = []
            for text in texts:
                result = self.sentiment_analyzer(text[:512])[0]
                sentiments.append({
                    'text': text[:100],
                    'label': result['label'],
                    'score': result['score']
                })

            positive = len([s for s in sentiments if s['label'] == 'POSITIVE'])
            negative = len([s for s in sentiments if s['label'] == 'NEGATIVE'])

            return {
                'total': len(texts),
                'positive': positive,
                'negative': negative,
                'positive_percent': round((positive / len(texts)) * 100, 2),
                'sentiments': sentiments
            }

        except Exception as e:
            logger.error(f"Feedback analysis failed: {e}")
            return {'error': str(e)}

    def compare_metrics(self, metrics_before: Dict, metrics_after: Dict) -> Dict[str, Any]:
        """
        Compare before/after metrics

        Args:
            metrics_before: Metrics before change
            metrics_after: Metrics after change

        Returns:
            Comparison with percent changes
        """
        logger.info("Comparing metrics...")

        comparison = {
            'timestamp': datetime.utcnow().isoformat(),
            'changes': {}
        }

        # Define key metrics to compare
        key_metrics = ['dau', 'mau', 'arpu', 'revenue', 'churn_rate',
                       'gen_success_rate', 'credit_usage', 'avg_session_iterations']

        for metric in key_metrics:
            before = metrics_before.get(metric, 0)
            after = metrics_after.get(metric, 0)

            if before > 0:
                change = ((after - before) / before) * 100
                comparison['changes'][metric] = {
                    'before': before,
                    'after': after,
                    'change_percent': round(change, 2),
                    'direction': 'up' if change > 0 else 'down' if change < 0 else 'flat'
                }
            else:
                comparison['changes'][metric] = {
                    'before': before,
                    'after': after,
                    'change_percent': 0,
                    'direction': 'new'
                }

        return comparison

    def export_metrics(self, format: str = 'json', filepath: Optional[str] = None) -> str:
        """
        Export metrics to file

        Args:
            format: Export format (json, csv)
            filepath: Output file path

        Returns:
            File path
        """
        logger.info(f"Exporting metrics as {format}...")

        try:
            # Get latest metrics
            metrics = self.db.get_latest_metrics(limit=100)

            if format == 'json':
                output = json.dumps(metrics, indent=2, default=str)
                ext = 'json'
            elif format == 'csv':
                df = pd.DataFrame(metrics)
                output = df.to_csv(index=False)
                ext = 'csv'
            else:
                raise ValueError(f"Unsupported format: {format}")

            # Determine output path
            if not filepath:
                timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                filepath = f"logs/metrics_export_{timestamp}.{ext}"

            # Write to file
            with open(filepath, 'w') as f:
                f.write(output)

            logger.info(f"Metrics exported to {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"Export failed: {e}")
            raise


# Singleton instance
_data_collector = None

def get_data_collector() -> DataCollector:
    """Get or create data collector singleton"""
    global _data_collector
    if _data_collector is None:
        _data_collector = DataCollector()
    return _data_collector
