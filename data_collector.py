"""
Data & Feedback Collector
Collects metrics, user feedback, and analyzes data post-deployment
"""

import os
import json
import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import stripe
import pandas as pd
from transformers import pipeline
from dotenv import load_dotenv
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

            # Calculate derived metrics
            metrics['calculated'] = self._calculate_derived_metrics(metrics)

            # Create summary
            summary = {
                'strategy_id': strategy_id,
                'cycle_id': cycle_id,
                'timestamp': datetime.utcnow().isoformat(),
                'dau': metrics['app_usage'].get('dau'),
                'mau': metrics['app_usage'].get('mau'),
                'arpu': metrics['calculated'].get('arpu'),
                'revenue': metrics['stripe'].get('revenue'),
                'churn_rate': metrics['calculated'].get('churn_rate'),
                'credit_usage': metrics['app_usage'].get('total_credits_used'),
                'gen_success_rate': metrics['app_usage'].get('gen_success_rate'),
                'avg_session_iterations': metrics['app_usage'].get('avg_session_iterations'),
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
        """Collect revenue and payment metrics from Stripe"""
        logger.info("Collecting Stripe metrics...")

        if not stripe.api_key:
            return {'error': 'Stripe not configured'}

        try:
            # Get recent charges (last 7 days)
            seven_days_ago = int((datetime.utcnow() - timedelta(days=7)).timestamp())

            charges = stripe.Charge.list(
                created={'gte': seven_days_ago},
                limit=100
            )

            # Calculate metrics
            total_revenue = sum(c.amount / 100 for c in charges.data if c.paid)
            successful_charges = len([c for c in charges.data if c.paid])
            failed_charges = len([c for c in charges.data if not c.paid])

            # Get customer count
            customers = stripe.Customer.list(limit=100)
            active_customers = len([c for c in customers.data if not c.get('deleted')])

            # Get subscriptions
            subscriptions = stripe.Subscription.list(limit=100)
            active_subscriptions = len([s for s in subscriptions.data if s.status == 'active'])

            return {
                'revenue': total_revenue,
                'successful_charges': successful_charges,
                'failed_charges': failed_charges,
                'active_customers': active_customers,
                'active_subscriptions': active_subscriptions,
                'period_days': 7
            }

        except Exception as e:
            logger.error(f"Stripe metrics collection failed: {e}")
            return {'error': str(e)}

    def _collect_app_usage_metrics(self) -> Dict[str, Any]:
        """Collect app usage metrics from Gentube API or database"""
        logger.info("Collecting app usage metrics...")

        try:
            # In a real implementation, you would query your app's database or API
            # For MVP, we'll return simulated structure

            # Example: Query Supabase tables for Gentube app data
            # This assumes you have user activity logged in Supabase

            # Simulated metrics structure
            metrics = {
                'dau': 0,  # Daily active users
                'mau': 0,  # Monthly active users
                'total_generations': 0,
                'successful_generations': 0,
                'failed_generations': 0,
                'gen_success_rate': 0.0,
                'total_credits_used': 0,
                'avg_session_iterations': 0.0,
                'unique_users_today': 0,
                'unique_users_month': 0,
                'avg_generation_time': 0.0,
                'studios_usage': {}  # Usage by studio type
            }

            # If Gentube API is available, fetch real data
            if self.gentube_api:
                # Example API call structure
                # response = requests.get(f"{self.gentube_api}/metrics")
                # metrics = response.json()
                pass

            return metrics

        except Exception as e:
            logger.error(f"App usage metrics collection failed: {e}")
            return {'error': str(e)}

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
