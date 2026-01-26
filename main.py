#!/usr/bin/env python3
"""
Gentube.ai Optimization Machine
Main entry point

Usage:
    python main.py run          # Run a single optimization cycle
    python main.py continuous   # Run continuous optimization loop
    python main.py bot          # Run Telegram bot only (for testing)
    python main.py health       # Check system health
    python main.py export       # Export metrics
"""

import os
import sys
import argparse
import logging
from dotenv import load_dotenv

# Load environment before imports
load_dotenv('config.env')

from loop_controller import get_loop_controller
from mobile_interface import get_mobile_interface
from safety_net import get_safety_net
from data_collector import get_data_collector
from db_handler import get_db_handler

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/main.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def run_single_cycle():
    """Run a single optimization cycle"""
    logger.info("Running single optimization cycle...")

    try:
        controller = get_loop_controller()
        result = controller.run_cycle()

        print("\n" + "="*60)
        print(f"Cycle ID: {result['cycle_id']}")
        print(f"Status: {result['status']}")
        print(f"Started: {result['started_at']}")
        print(f"Completed: {result.get('completed_at', 'N/A')}")
        print("="*60)

        # Print stage results
        print("\nStage Results:")
        for stage_name, stage_result in result.get('stages', {}).items():
            status = '✅' if stage_result.get('success', False) else '❌'
            print(f"  {status} {stage_name}")

        if result.get('error'):
            print(f"\n❌ Error: {result['error']}")

        print("\n")

        return 0 if result['status'] in ['success', 'rejected', 'timeout'] else 1

    except Exception as e:
        logger.error(f"Failed to run cycle: {e}", exc_info=True)
        print(f"\n❌ Failed to run cycle: {e}\n")
        return 1


def run_continuous():
    """Run continuous optimization loop"""
    logger.info("Starting continuous optimization mode...")

    try:
        print("\n" + "="*60)
        print("Gentube.ai Optimization Machine")
        print("Running in continuous mode")
        print("Press Ctrl+C to stop")
        print("="*60 + "\n")

        controller = get_loop_controller()
        controller.run_continuous()

        return 0

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping...")
        print("\n\nStopping optimization loop...\n")
        controller = get_loop_controller()
        controller.stop()
        return 0

    except Exception as e:
        logger.error(f"Continuous mode failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}\n")
        return 1


def run_bot():
    """Run Telegram bot only (for testing)"""
    logger.info("Running Telegram bot in standalone mode...")

    try:
        print("\n" + "="*60)
        print("Telegram Bot - Standalone Mode")
        print("Press Ctrl+C to stop")
        print("="*60 + "\n")

        bot = get_mobile_interface()
        bot.run_polling()

        return 0

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping bot...")
        print("\n\nStopping bot...\n")
        return 0

    except Exception as e:
        logger.error(f"Bot failed: {e}", exc_info=True)
        print(f"\n❌ Bot failed: {e}\n")
        return 1


def check_health():
    """Check system health"""
    logger.info("Performing health check...")

    try:
        print("\n" + "="*60)
        print("System Health Check")
        print("="*60 + "\n")

        safety = get_safety_net()
        health = safety.check_health()

        print(f"Overall Status: {health['status'].upper()}")
        print(f"Timestamp: {health['timestamp']}")
        print("\nComponent Status:")

        for component, status in health.get('components', {}).items():
            if isinstance(status, dict):
                print(f"  • {component}: {status.get('status', 'unknown')}")
                if 'unacknowledged_count' in status:
                    print(f"    Unacknowledged Alerts: {status['unacknowledged_count']}")
            else:
                print(f"  • {component}: {status}")

        if health.get('error'):
            print(f"\n❌ Error: {health['error']}")

        print("\n")

        return 0 if health['status'] == 'healthy' else 1

    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        print(f"\n❌ Health check failed: {e}\n")
        return 1


def export_metrics(format='json'):
    """Export metrics to file"""
    logger.info(f"Exporting metrics as {format}...")

    try:
        print("\n" + "="*60)
        print(f"Exporting Metrics ({format.upper()})")
        print("="*60 + "\n")

        collector = get_data_collector()
        filepath = collector.export_metrics(format=format)

        print(f"✅ Metrics exported to: {filepath}\n")

        return 0

    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        print(f"\n❌ Export failed: {e}\n")
        return 1


def show_stats():
    """Show system statistics"""
    logger.info("Fetching statistics...")

    try:
        print("\n" + "="*60)
        print("System Statistics")
        print("="*60 + "\n")

        db = get_db_handler()

        # Get pending strategies
        pending = db.get_pending_strategies()
        print(f"Pending Strategies: {len(pending)}")

        # Get recent metrics
        metrics = db.get_latest_metrics(limit=1)
        if metrics:
            latest = metrics[0]
            print(f"\nLatest Metrics ({latest['timestamp']}):")
            print(f"  • DAU: {latest.get('dau', 'N/A')}")
            print(f"  • Revenue: ${latest.get('revenue', 0):.2f}")
            print(f"  • ARPU: ${latest.get('arpu', 0):.2f}")
            print(f"  • Gen Success Rate: {latest.get('gen_success_rate', 0):.1f}%")

        # Get unacknowledged alerts
        alerts = db.get_unacknowledged_alerts()
        print(f"\nUnacknowledged Alerts: {len(alerts)}")

        if alerts:
            print("\nRecent Alerts:")
            for alert in alerts[:5]:
                severity_emoji = {'low': 'ℹ️', 'medium': '⚠️', 'high': '🚨', 'critical': '🔥'}
                emoji = severity_emoji.get(alert['severity'], 'ℹ️')
                print(f"  {emoji} [{alert['severity'].upper()}] {alert['message'][:60]}...")

        print("\n")

        return 0

    except Exception as e:
        logger.error(f"Stats failed: {e}", exc_info=True)
        print(f"\n❌ Failed to fetch stats: {e}\n")
        return 1


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Gentube.ai Optimization Machine',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'command',
        choices=['run', 'continuous', 'bot', 'health', 'export', 'stats'],
        help='Command to execute'
    )

    parser.add_argument(
        '--format',
        choices=['json', 'csv'],
        default='json',
        help='Export format (for export command)'
    )

    args = parser.parse_args()

    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)

    # Execute command
    if args.command == 'run':
        return run_single_cycle()
    elif args.command == 'continuous':
        return run_continuous()
    elif args.command == 'bot':
        return run_bot()
    elif args.command == 'health':
        return check_health()
    elif args.command == 'export':
        return export_metrics(format=args.format)
    elif args.command == 'stats':
        return show_stats()
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
