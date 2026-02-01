"""
Iteration Loop Controller
Orchestrates the full optimization cycle
"""

import os
import logging
import asyncio
import schedule
import time
from typing import Dict, Optional, Any
from datetime import datetime
from dotenv import load_dotenv

from db_handler import get_db_handler
from strategy_engine import get_strategy_engine
from mobile_interface import get_mobile_interface
from execution_agent import get_execution_agent
from data_collector import get_data_collector
from safety_net import get_safety_net
from code_cache import get_code_cache

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/loop_controller.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class LoopController:
    """Main loop controller for the optimization system"""

    def __init__(self):
        self.db = get_db_handler()
        self.strategy_engine = get_strategy_engine()
        self.mobile = get_mobile_interface()
        self.executor = get_execution_agent()
        self.collector = get_data_collector()
        self.safety = get_safety_net()
        self.code_cache = get_code_cache()

        # Configuration
        self.cycle_interval_hours = int(os.getenv('CYCLE_INTERVAL_HOURS', 12))
        self.approval_timeout_hours = int(os.getenv('APPROVAL_TIMEOUT_HOURS', 24))

        # State
        self.is_running = False
        self.current_cycle_id = None

        # Load saved LLM provider preference from DB
        saved_provider = self.db.get_setting('llm_provider')
        if saved_provider:
            try:
                self.strategy_engine.set_llm_provider(saved_provider)
                logger.info(f"Loaded saved LLM provider: {saved_provider}")
            except ValueError as e:
                logger.warning(f"Could not restore saved LLM provider: {e}")

        logger.info("Loop controller initialized")

    @staticmethod
    def _get_next_mrr_milestone(current_mrr: float) -> int:
        """Determine the next MRR milestone based on current MRR.

        Milestones: $1k, $3k, $5k, $10k, $20k...$100k (by $10k),
        $150k...$500k (by $50k), $600k...$1M (by $100k),
        $1.2M...$2M (by $200k), $3M...$10M (by $1M).
        """
        milestones = [1000, 3000, 5000]
        milestones += list(range(10000, 100001, 10000))       # 10k-100k by 10k
        milestones += list(range(150000, 500001, 50000))      # 150k-500k by 50k
        milestones += list(range(600000, 1000001, 100000))    # 600k-1M by 100k
        milestones += list(range(1200000, 2000001, 200000))   # 1.2M-2M by 200k
        milestones += list(range(3000000, 10000001, 1000000)) # 3M-10M by 1M

        for m in milestones:
            if current_mrr < m:
                return m
        return 10000000

    def _wait_for_ceo_action(self, timeout_minutes: int = 60) -> Optional[Dict]:
        """
        Send landing menu and wait for CEO to choose an action.

        Returns:
            Dict with 'action' key:
            - {'action': 'restart'} - Generate new strategy
            - {'action': 'execute', 'strategy_id': int} - Execute specific strategy
            - None if timeout
        """
        chat_id = int(self.mobile.chat_id) if self.mobile.chat_id else None
        if not chat_id:
            logger.error("No chat_id configured, skipping landing menu")
            return {'action': 'restart'}  # Default to restart if no chat configured

        # Check if there's already a pending selection (e.g., CEO used /restart between cycles)
        existing_selection = self.mobile.get_menu_selection(chat_id)
        if existing_selection:
            if existing_selection == 'restart':
                logger.info(f"CEO already selected: {existing_selection}")
                self.mobile.clear_menu_selection(chat_id)
                return {'action': 'restart'}
            elif existing_selection.startswith('execute:'):
                logger.info(f"CEO already selected: {existing_selection}")
                self.mobile.clear_menu_selection(chat_id)
                strategy_id = int(existing_selection.split(':')[1])
                return {'action': 'execute', 'strategy_id': strategy_id}

        # Clear any previous selection
        self.mobile.clear_menu_selection(chat_id)

        # Send bot intro then landing menu
        logger.info("Sending welcome intro and landing menu to CEO...")
        asyncio.run(self.mobile.send_welcome_intro())
        asyncio.run(self.mobile.send_landing_menu())

        # Poll for selection
        start_time = time.time()
        timeout_seconds = timeout_minutes * 60

        logger.info(f"Waiting for CEO action (timeout: {timeout_minutes} minutes)...")

        last_logged_selection = None  # Track to avoid spam logging

        while time.time() - start_time < timeout_seconds:
            selection = self.mobile.get_menu_selection(chat_id)

            if selection:
                if selection == 'restart':
                    logger.info(f"CEO selected: {selection}")
                    return {'action': 'restart'}

                elif selection.startswith('execute:'):
                    logger.info(f"CEO selected: {selection}")
                    strategy_id = int(selection.split(':')[1])
                    return {'action': 'execute', 'strategy_id': strategy_id}

                # For saved/failed/rejected/approved, the user is browsing lists
                # Log once and keep waiting for a final action
                if selection != last_logged_selection:
                    logger.info(f"CEO browsing: {selection} (waiting for exec/restart...)")
                    last_logged_selection = selection

            time.sleep(5)  # Poll every 5 seconds

        logger.warning("CEO action timeout")
        return None

    def run_cycle(self) -> Dict[str, Any]:
        """
        Run a single optimization cycle

        Returns:
            Cycle result summary
        """
        cycle_id = f"cycle_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        self.current_cycle_id = cycle_id

        logger.info(f"========== Starting Cycle: {cycle_id} ==========")

        cycle_result = {
            'cycle_id': cycle_id,
            'started_at': datetime.utcnow().isoformat(),
            'status': 'running',
            'stages': {}
        }

        try:
            # Send landing menu and wait for CEO to choose what to do
            ceo_action = self._wait_for_ceo_action(timeout_minutes=60)

            if ceo_action is None:
                # Timeout - end cycle
                logger.warning("CEO action timeout, ending cycle")
                cycle_result['status'] = 'timeout'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Handle CEO's choice
            if ceo_action['action'] == 'execute':
                # CEO selected a specific strategy to execute
                strategy_id = ceo_action['strategy_id']
                strategy = self.db.get_strategy(strategy_id)

                if not strategy:
                    raise Exception(f"Strategy {strategy_id} not found")

                logger.info(f"CEO selected strategy {strategy_id} for execution")
                self.mobile.send_status_update_sync(
                    f"▶️ **Executing Strategy #{strategy_id}**\n"
                    f"Skipping to validation and code generation..."
                )

                # Set up cycle result with the selected strategy
                cycle_result['stages']['data_collection'] = {
                    'success': True,
                    'data': {'metrics': {}, 'feedback': [], 'goals': {}, 'current_features': [], 'constraints': {}},
                    'ceo_selected': True
                }
                cycle_result['stages']['strategy_generation'] = {
                    'success': True,
                    'strategy': strategy,
                    'strategy_id': strategy_id,
                    'ceo_selected': True
                }
                cycle_result['stages']['approval'] = {
                    'success': True,
                    'auto_execute': True,
                    'approval': {'response_type': 'approve'},
                    'ceo_selected': True
                }

                # Update strategy status to approved
                self.db.update_strategy_status(strategy_id, 'approved')
                self.db.update_execution_stage(strategy_id, 'waiting_approval')

                # Skip to validation and execution (after the approval stage)
                # Jump to Stage 4
                logger.info("Skipping to validation stage (CEO pre-approved)")

            elif ceo_action['action'] == 'restart':
                # CEO wants a new strategy - normal flow
                logger.info("CEO selected restart - generating new strategy")

                # Check if there's a context message from "Run as New Strategy" button
                ceo_context = self.mobile.get_strategy_generation_context()

                # Stage 1: Collect current metrics and feedback
                logger.info("Stage 1: Collecting current data...")
                self.mobile.send_status_update_sync("⏳ **Stage 1/7: Collecting Data**\nGathering current metrics and feedback...")
                cycle_result['stages']['data_collection'] = self._stage_collect_data()

                if not cycle_result['stages']['data_collection']['success']:
                    raise Exception("Data collection failed")

                # Stage 2: Generate strategy
                logger.info("Stage 2: Generating optimization strategy...")
                self.mobile.send_status_update_sync("⏳ **Stage 2/7: Generating Strategy**\nAnalyzing data and creating optimization strategy...")
                cycle_result['stages']['strategy_generation'] = self._stage_generate_strategy(
                    cycle_result['stages']['data_collection']['data'],
                    ceo_context=ceo_context
                )

                if not cycle_result['stages']['strategy_generation']['success']:
                    raise Exception("Strategy generation failed")

                # Stage 3: Request approval (with tweak/refinement loop)
                # Only run if approval wasn't already set (e.g., by CEO selecting 'execute')
                logger.info("Stage 3: Requesting approval...")
                self.mobile.send_status_update_sync("⏳ **Stage 3/7: Requesting Approval**\nSending strategy proposal for your review...")
                cycle_result['stages']['approval'] = self._stage_request_approval(
                    cycle_result['stages']['strategy_generation']['strategy'],
                    data=cycle_result['stages']['data_collection']['data']
                )

                # Update strategy reference if it was refined
                if cycle_result['stages']['approval'].get('strategy'):
                    cycle_result['stages']['strategy_generation']['strategy'] = \
                        cycle_result['stages']['approval']['strategy']

                if not cycle_result['stages']['approval']['success']:
                    logger.info(f"Approval stage result: {cycle_result['stages']['approval']['status']}")
                    if cycle_result['stages']['approval']['status'] == 'rejected':
                        cycle_result['status'] = 'rejected'
                    elif cycle_result['stages']['approval']['status'] == 'timeout':
                        cycle_result['status'] = 'timeout'
                    else:
                        cycle_result['status'] = 'cancelled'

                    self._finalize_cycle(cycle_id, cycle_result)
                    return cycle_result

                # Check if this is a save-only approval (no auto-execution)
                if not cycle_result['stages']['approval'].get('auto_execute', False):
                    logger.info("Strategy saved for manual implementation")
                    cycle_result['status'] = 'saved'
                    self._finalize_cycle(cycle_id, cycle_result)
                    return cycle_result

            # Stage 4: Validate changes (only for auto-execution)
            logger.info("Stage 4: Validating changes...")
            self.mobile.send_status_update_sync("⏳ **Stage 4/7: Validating Changes**\nRunning safety checks...")
            cycle_result['stages']['validation'] = self._stage_validate_changes(
                cycle_result['stages']['strategy_generation']['strategy']
            )

            if not cycle_result['stages']['validation']['safe']:
                logger.warning("Changes failed safety validation")
                cycle_result['status'] = 'unsafe'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 4.5: Generate code plan
            logger.info("Stage 4.5: Generating code plan...")
            strategy_id_for_msg = cycle_result['stages']['strategy_generation'].get('strategy_id') or cycle_result['stages']['strategy_generation']['strategy'].get('id', '?')
            self.mobile.send_status_update_sync(f"⏳ **Stage 4.5/7: Code Planning**\nStrategy #{strategy_id_for_msg} — Generating implementation plan...")
            cycle_result['stages']['code_plan'] = self._stage_generate_code_plan(
                cycle_result['stages']['strategy_generation']['strategy']
            )

            if not cycle_result['stages']['code_plan']['success']:
                logger.warning("Code plan generation failed, proceeding without plan")

            # Stage 5: Generate code and deploy to feature branch
            logger.info("Stage 5: Generating code and deploying to preview...")
            self.mobile.send_status_update_sync(f"⏳ **Stage 5/7: Code Generation & Deploy**\nStrategy #{strategy_id_for_msg} — Generating code and deploying to preview branch...")
            cycle_result['stages']['execution'] = self._stage_execute_to_preview(
                cycle_result['stages']['strategy_generation']['strategy'],
                cycle_result['stages']['approval']['approval']
            )

            if not cycle_result['stages']['execution']['success']:
                error_msg = cycle_result['stages']['execution'].get('error', 'Unknown execution error')
                raise Exception(f"Execution failed: {error_msg}")

            # Stage 6: Wait for merge approval
            logger.info("Stage 6: Waiting for merge approval...")
            self.mobile.send_status_update_sync("⏳ **Stage 6/7: Merge Approval**\nPreview ready — sending for your review...")
            cycle_result['stages']['merge_approval'] = self._stage_wait_for_merge(
                cycle_result['stages']['strategy_generation']['strategy']['id'],
                cycle_result['stages']['execution']['preview_url'],
                cycle_result['stages']['execution']['branch_name'],
                cycle_result['stages']['execution']['changes_summary'],
                cycle_result['stages']['execution'].get('github_url'),
                cycle_result['stages']['execution'].get('vercel_deployment_url')
            )

            if not cycle_result['stages']['merge_approval']['success']:
                if cycle_result['stages']['merge_approval']['status'] == 'discarded':
                    logger.info("Preview discarded by user")
                    cycle_result['status'] = 'discarded'
                else:
                    cycle_result['status'] = 'merge_failed'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 7: Monitor deployment
            logger.info("Stage 7: Monitoring deployment...")
            self.mobile.send_status_update_sync("⏳ **Stage 7/7: Monitoring Deployment**\nWatching for issues post-merge...")
            cycle_result['stages']['monitoring'] = self._stage_monitor_deployment(
                cycle_id,
                cycle_result['stages']['strategy_generation']['strategy']['id'],
                cycle_result['stages']['data_collection']['data']['metrics']
            )

            # Check if rollback needed
            if cycle_result['stages']['monitoring'].get('should_rollback'):
                logger.warning("Monitoring triggered rollback")
                cycle_result['status'] = 'rolled_back'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 7: Collect post-deployment metrics
            logger.info("Stage 7: Collecting post-deployment metrics...")
            cycle_result['stages']['post_metrics'] = self._stage_collect_post_metrics(
                cycle_result['stages']['strategy_generation']['strategy']['id'],
                cycle_id
            )

            # Stage 8: Evaluate success
            logger.info("Stage 8: Evaluating strategy success...")
            cycle_result['stages']['evaluation'] = self._stage_evaluate_success(
                cycle_result['stages']['data_collection']['data']['metrics'],
                cycle_result['stages']['post_metrics']['data']['metrics'],
                cycle_result['stages']['strategy_generation']['strategy'].get('expected_impact', '')
            )

            # Determine final status
            if cycle_result['stages']['evaluation']['success']:
                cycle_result['status'] = 'success'
            else:
                cycle_result['status'] = 'completed_unsuccessful'

            logger.info(f"Cycle {cycle_id} completed: {cycle_result['status']}")

        except Exception as e:
            logger.error(f"Cycle failed: {e}", exc_info=True)
            cycle_result['status'] = 'failed'
            cycle_result['error'] = str(e)

            # Send failure notification to CEO via Telegram
            try:
                self.mobile.send_alert_sync(
                    f"❌ **Cycle Failed**\n\n"
                    f"**Cycle:** {cycle_id}\n"
                    f"**Error:** {e}\n\n"
                    f"Check logs for details.",
                    severity='high'
                )
            except Exception as notify_err:
                logger.error(f"Failed to send failure notification: {notify_err}")

            # Create alert in database
            self.safety._create_alert(
                alert_type='cycle_failure',
                message=f"Cycle {cycle_id} failed: {e}",
                severity='high',
                cycle_id=cycle_id
            )

        finally:
            cycle_result['completed_at'] = datetime.utcnow().isoformat()
            self._finalize_cycle(cycle_id, cycle_result)

        logger.info(f"========== Cycle {cycle_id} Finished ==========")
        return cycle_result

    def _handle_stuck_strategies(self, stuck_strategies: list, cycle_result: Dict) -> Optional[Dict]:
        """
        Handle strategies that are stuck in intermediate states.
        Auto-resumes strategies that were re-approved (e.g. from /failed or /rejected).
        For others, asks CEO which one to resume.

        Returns:
            cycle_result if resuming a strategy, None to continue with new cycle
        """
        # Check for strategies that were re-approved and should auto-resume
        # These have status='approved' and an existing 'approve' approval record
        for s in stuck_strategies:
            if s.get('status') == 'approved':
                approval = self.db.get_approval(s['id'])
                if approval and approval.get('response_type') == 'approve':
                    logger.info(f"Auto-resuming re-approved strategy {s['id']} from stage {s.get('execution_stage')}")
                    return self._resume_strategy(s, cycle_result)

        # Build message about stuck strategies
        msg_lines = ["🔄 **Stuck Strategies Detected**\n"]
        msg_lines.append("The following strategies are in intermediate states:\n")

        for s in stuck_strategies:
            stage = s.get('execution_stage', 'unknown')
            status = s.get('status', 'unknown')
            summary = s.get('summary', 'No summary')[:100]
            created = s.get('created_at', '')[:10]

            msg_lines.append(f"**ID {s['id']}** - {status}/{stage}")
            msg_lines.append(f"  {summary}...")
            msg_lines.append(f"  Created: {created}\n")

        msg_lines.append("\nReply with a strategy ID to resume, or 'skip' to start a new cycle.")

        # Send notification
        try:
            self.mobile.send_alert_sync("\n".join(msg_lines), severity='medium')
        except Exception as e:
            logger.error(f"Failed to send stuck strategies notification: {e}")
            return None

        # Wait for CEO response (shorter timeout for resume decision)
        logger.info("Waiting for CEO decision on stuck strategies...")

        # Poll for a text response (strategy ID or 'skip')
        start_time = time.time()
        start_timestamp = datetime.utcnow().isoformat()  # For filtering new approvals
        timeout_seconds = 300  # 5 minute timeout for resume decision

        while time.time() - start_time < timeout_seconds:
            # Check for any approval response on any of the stuck strategies
            for s in stuck_strategies:
                # Look for approvals created after the wait started
                approval = self.db.get_approval(s['id'], after_timestamp=start_timestamp)
                if approval and approval.get('response_type') == 'approve':
                    # Resume this strategy
                    logger.info(f"Resuming strategy {s['id']} from stage {s.get('execution_stage')}")
                    return self._resume_strategy(s, cycle_result)

            time.sleep(15)  # Poll every 15 seconds

        # Timeout - continue with new cycle
        logger.info("No resume response received, continuing with new cycle")
        return None

    def _resume_strategy(self, strategy: Dict, cycle_result: Dict) -> Dict:
        """
        Resume a stuck strategy from its last known stage.

        Args:
            strategy: The strategy to resume
            cycle_result: Current cycle result to populate

        Returns:
            Completed cycle result
        """
        strategy_id = strategy['id']
        status = strategy.get('status', '')
        stage = strategy.get('execution_stage', '')
        metadata = self.db.get_strategy_execution_metadata(strategy_id) or {}

        logger.info(f"Resuming strategy {strategy_id} from status={status}, stage={stage}")

        # Set up cycle result with resumed strategy
        cycle_result['stages']['data_collection'] = {
            'success': True,
            'data': {'metrics': {}, 'feedback': [], 'goals': {}, 'current_features': [], 'constraints': {}},
            'resumed': True
        }
        cycle_result['stages']['strategy_generation'] = {
            'success': True,
            'strategy': strategy,
            'strategy_id': strategy_id,
            'resumed': True
        }
        cycle_result['stages']['approval'] = {
            'success': True,
            'auto_execute': True,
            'approval': {'response_type': 'approve'},
            'resumed': True
        }

        try:
            # Determine where to resume based on status and stage
            if status == 'approved':
                # Need to generate code and deploy
                if stage in ['', 'waiting_approval', 'generating_code']:
                    logger.info("Resuming: Code generation and deployment")
                    return self._resume_from_code_generation(strategy, cycle_result, metadata)
                elif stage == 'pushing_branch':
                    logger.info("Resuming: Push to branch")
                    return self._resume_from_code_generation(strategy, cycle_result, metadata)

            elif status == 'preview':
                # Code is deployed, waiting for merge
                if stage in ['waiting_merge', '']:
                    logger.info("Resuming: Waiting for merge approval")
                    return self._resume_from_merge_wait(strategy, cycle_result, metadata)
                elif stage == 'merging':
                    logger.info("Resuming: Merge in progress")
                    return self._resume_from_merge_wait(strategy, cycle_result, metadata)

            # Default: try to resume from code generation
            logger.warning(f"Unknown resume state, attempting from code generation")
            return self._resume_from_code_generation(strategy, cycle_result, metadata)

        except Exception as e:
            logger.error(f"Resume failed: {e}", exc_info=True)
            cycle_result['status'] = 'resume_failed'
            cycle_result['error'] = str(e)
            self.mobile.send_alert_sync(
                f"❌ **Resume Failed**\n\n"
                f"Strategy {strategy_id} could not be resumed.\n"
                f"Error: {e}",
                severity='high'
            )
            return cycle_result

    def _resume_from_code_generation(self, strategy: Dict, cycle_result: Dict, metadata: Dict) -> Dict:
        """Resume from code generation stage"""
        strategy_id = strategy['id']

        # Update stage
        self.db.update_execution_stage(strategy_id, 'generating_code')

        # Stage 5: Generate code and deploy
        logger.info("Stage 5 (resumed): Generating code and deploying to preview...")
        cycle_result['stages']['execution'] = self._stage_execute_to_preview(
            strategy,
            cycle_result['stages']['approval']['approval']
        )

        if not cycle_result['stages']['execution']['success']:
            error_msg = cycle_result['stages']['execution'].get('error', 'Unknown execution error')
            raise Exception(f"Execution failed: {error_msg}")

        # Continue to merge wait
        return self._continue_to_merge_wait(strategy, cycle_result)

    def _resume_from_merge_wait(self, strategy: Dict, cycle_result: Dict, metadata: Dict) -> Dict:
        """Resume from merge wait stage - preview already deployed"""
        strategy_id = strategy['id']

        # Get metadata for preview URL, branch name, etc.
        branch_name = metadata.get('branch_name', f"optimize/strategy-{strategy_id}")
        preview_url = metadata.get('preview_url', '')
        github_url = metadata.get('github_url', '')
        vercel_deployment_url = metadata.get('vercel_deployment_url', '')
        changes_summary = metadata.get('changes_summary', 'Code changes applied')

        # Populate execution stage result from metadata
        cycle_result['stages']['execution'] = {
            'success': True,
            'branch_name': branch_name,
            'preview_url': preview_url,
            'github_url': github_url,
            'vercel_deployment_url': vercel_deployment_url,
            'changes_summary': changes_summary,
            'resumed': True
        }

        # Send notification that we're resuming
        self.mobile.send_alert_sync(
            f"🔄 **Resuming Strategy #{strategy_id}**\n\n"
            f"Preview: {preview_url}\n"
            f"Branch: {branch_name}\n\n"
            f"Waiting for merge/discard decision...",
            severity='low'
        )

        return self._continue_to_merge_wait(strategy, cycle_result)

    def _continue_to_merge_wait(self, strategy: Dict, cycle_result: Dict) -> Dict:
        """Continue the cycle from merge wait stage"""
        strategy_id = strategy['id']
        execution = cycle_result['stages']['execution']

        # Stage 6: Wait for merge approval
        logger.info("Stage 6: Waiting for merge approval...")
        cycle_result['stages']['merge_approval'] = self._stage_wait_for_merge(
            strategy_id,
            execution['preview_url'],
            execution['branch_name'],
            execution['changes_summary'],
            execution.get('github_url'),
            execution.get('vercel_deployment_url')
        )

        if not cycle_result['stages']['merge_approval']['success']:
            if cycle_result['stages']['merge_approval']['status'] == 'discarded':
                cycle_result['status'] = 'discarded'
            else:
                cycle_result['status'] = 'merge_failed'
            self._finalize_cycle(self.current_cycle_id, cycle_result)
            return cycle_result

        # Mark as completed and clean up cache
        self.db.update_execution_stage(strategy_id, 'completed')
        self.db.update_strategy_status(strategy_id, 'completed')
        self.code_cache.delete(strategy_id)
        cycle_result['status'] = 'success'
        self._finalize_cycle(self.current_cycle_id, cycle_result)

        return cycle_result

    def _stage_collect_data(self) -> Dict[str, Any]:
        """Stage 1: Collect current data"""
        try:
            # Collect metrics
            metrics = self.collector.collect_metrics(wait_hours=0)

            # Get recent feedback
            feedback = self.db.get_recent_feedback(days=7, limit=50)

            # Determine dynamic MRR milestone based on current MRR
            stripe_data = metrics.get('kpi_data', {}).get('stripe', {})
            current_mrr = stripe_data.get('mrr', 0)
            target_mrr = self._get_next_mrr_milestone(current_mrr)

            goals = {
                'current_mrr': current_mrr,
                'target_mrr': target_mrr,
                'target_arpu': 20,
                'target_retention': 85
            }

            # Get current features (this would come from your app config)
            current_features = [
                'AI Image Generation',
                'AI Video Generation',
                'Multiple Studio Styles',
                'Credit System'
            ]

            # Get constraints
            constraints = {
                'risk_tolerance': 'Low',
                'budget': 1000,
                'time_horizon': '1-2 weeks'
            }

            return {
                'success': True,
                'data': {
                    'metrics': metrics,
                    'feedback': feedback,
                    'goals': goals,
                    'current_features': current_features,
                    'constraints': constraints
                }
            }

        except Exception as e:
            logger.error(f"Data collection failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_generate_strategy(self, data: Dict, ceo_context: str = None) -> Dict[str, Any]:
        """Stage 2: Generate optimization strategy"""
        try:
            # Generate strategy using AI
            strategy = self.strategy_engine.generate_strategy(data, ceo_context=ceo_context)

            # Write to database
            strategy_id = self.db.write_strategy(strategy)
            strategy['id'] = strategy_id

            return {
                'success': True,
                'strategy': strategy,
                'strategy_id': strategy_id
            }

        except Exception as e:
            logger.error(f"Strategy generation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_request_approval(self, strategy: Dict, data: Dict = None,
                                  max_refinements: int = 3) -> Dict[str, Any]:
        """
        Stage 3: Request user approval with tweak/refinement loop

        Args:
            strategy: Current strategy to request approval for
            data: Original data context (for refinements)
            max_refinements: Maximum number of refinement iterations
        """
        refinement_count = 0
        current_strategy = strategy

        while refinement_count <= max_refinements:
            try:
                strategy_id = current_strategy['id']

                # Send proposal via mobile
                sent = asyncio.run(self.mobile.send_proposal(current_strategy))

                if not sent:
                    return {
                        'success': False,
                        'status': 'send_failed',
                        'error': 'Failed to send proposal'
                    }

                # Wait for approval
                logger.info(f"Waiting for approval (timeout: {self.approval_timeout_hours} hours)...")

                approval = self.db.wait_for_approval(
                    strategy_id,
                    timeout_minutes=self.approval_timeout_hours * 60,
                    poll_interval=300  # Check every 5 minutes
                )

                if not approval:
                    self.db.update_strategy_status(strategy_id, 'timeout')
                    return {
                        'success': False,
                        'status': 'timeout',
                        'message': 'Approval timeout'
                    }

                response_type = approval['response_type']

                if response_type == 'plan':
                    # Plan-only: plan is generated by mobile_interface, no auto-execution
                    self.db.update_strategy_status(strategy_id, 'saved')
                    return {
                        'success': True,
                        'status': 'saved',
                        'approval': approval,
                        'strategy': current_strategy,
                        'refinement_count': refinement_count,
                        'auto_execute': False
                    }

                elif response_type == 'save':
                    # Saved for manual implementation
                    self.db.update_strategy_status(strategy_id, 'saved')
                    return {
                        'success': True,
                        'status': 'saved',
                        'approval': approval,
                        'strategy': current_strategy,
                        'refinement_count': refinement_count,
                        'auto_execute': False
                    }

                elif response_type == 'approve':
                    # Approved for auto-execution (response_type='approve' with status='approved')
                    self.db.update_strategy_status(strategy_id, 'approved')
                    return {
                        'success': True,
                        'status': 'approved',
                        'approval': approval,
                        'strategy': current_strategy,
                        'refinement_count': refinement_count,
                        'auto_execute': True
                    }

                elif response_type == 'reject':
                    self.db.update_strategy_status(strategy_id, 'rejected')
                    return {
                        'success': False,
                        'status': 'rejected',
                        'approval': approval
                    }

                else:  # tweak
                    refinement_count += 1
                    user_feedback = approval.get('notes', approval.get('user_response', ''))

                    # Extract just the feedback text after "tweak:"
                    if user_feedback.lower().startswith('tweak:'):
                        user_feedback = user_feedback[6:].strip()

                    logger.info(f"Tweak requested (#{refinement_count}): {user_feedback[:50]}...")

                    if refinement_count > max_refinements:
                        logger.warning(f"Max refinements ({max_refinements}) reached")
                        self.db.update_strategy_status(strategy_id, 'max_refinements')
                        self.mobile.send_status_update_sync(
                            f"Max refinements ({max_refinements}) reached. Please approve or reject the current strategy, or start a new cycle."
                        )
                        return {
                            'success': False,
                            'status': 'max_refinements',
                            'message': f'Maximum refinements ({max_refinements}) reached'
                        }

                    # Mark original as replaced (using 'rejected' as superseded isn't a valid DB status)
                    self.db.update_strategy_status(strategy_id, 'rejected')

                    # Generate refined strategy
                    if data is None:
                        logger.warning("No data context for refinement, using empty context")
                        data = {}

                    refined_strategy = self.strategy_engine.refine_strategy(
                        current_strategy, user_feedback, data
                    )

                    # Save refined strategy to database
                    refined_id = self.db.write_strategy(refined_strategy)
                    refined_strategy['id'] = refined_id

                    # Notify user
                    self.mobile.send_status_update_sync(
                        f"Strategy refined based on your feedback. Sending new proposal..."
                    )

                    # Loop with refined strategy
                    current_strategy = refined_strategy

            except Exception as e:
                logger.error(f"Approval request failed: {e}")
                return {
                    'success': False,
                    'status': 'error',
                    'error': str(e)
                }

        # Should not reach here, but just in case
        return {
            'success': False,
            'status': 'error',
            'error': 'Unexpected loop exit'
        }

    def _stage_validate_changes(self, strategy: Dict) -> Dict[str, Any]:
        """Stage 4: Validate changes with safety net"""
        try:
            import json
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            # Get current metrics for validation
            latest_metrics = self.db.get_latest_metrics(limit=1)
            current_metrics = latest_metrics[0] if latest_metrics else {}

            # Validate
            validation = self.safety.validate_changes(changes, current_metrics)

            return validation

        except Exception as e:
            logger.error(f"Validation failed: {e}")
            return {
                'safe': False,
                'errors': [str(e)]
            }

    def _stage_generate_code_plan(self, strategy: Dict) -> Dict[str, Any]:
        """Stage 4.5: Generate a code plan and save it as MD to cache/code_plans/"""
        try:
            import json
            from pathlib import Path

            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            plan_result = self.strategy_engine.generate_code_plan(strategy, changes)

            if plan_result.get('success'):
                strategy_id = strategy.get('id', 'unknown')
                plans_dir = Path('cache/code_plans')
                plans_dir.mkdir(parents=True, exist_ok=True)

                plan_file = plans_dir / f"strategy_{strategy_id}_plan.md"
                with open(plan_file, 'w') as f:
                    f.write(plan_result['plan_md'])

                logger.info(f"Code plan saved to {plan_file}")
                return {
                    'success': True,
                    'plan_md': plan_result['plan_md'],
                    'plan_file': str(plan_file)
                }
            else:
                return {
                    'success': False,
                    'error': plan_result.get('error', 'Plan generation failed')
                }

        except Exception as e:
            logger.error(f"Code plan generation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_execute_changes(self, strategy: Dict, approval: Dict) -> Dict[str, Any]:
        """Stage 5: Execute approved changes"""
        try:
            import json
            strategy_id = strategy['id']
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            # Execute
            result = self.executor.execute_changes(strategy_id, changes, approval)

            # Update strategy status
            if result['success']:
                self.db.update_strategy_status(strategy_id, 'executed')
            else:
                self.db.update_strategy_status(strategy_id, 'failed')

            return result

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return {
                'success': False,
                'errors': [str(e)]
            }

    def _stage_execute_to_preview(self, strategy: Dict, approval: Dict) -> Dict[str, Any]:
        """Stage 5: Generate code and deploy to preview branch"""
        try:
            import json
            strategy_id = strategy['id']
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            # Track stage: generating code
            self.db.update_execution_stage(strategy_id, 'generating_code')

            # Check cache first to avoid redundant API calls
            cached_code = self.code_cache.load(strategy_id)
            if cached_code and cached_code.get('success'):
                logger.info(f"Using cached code changes for strategy {strategy_id}")
                code_result = cached_code
            else:
                # Generate code for the changes
                logger.info("Generating code for changes (calling Claude API)...")
                code_result = self.strategy_engine.generate_code(strategy, changes)

                if not code_result.get('success'):
                    self.db.update_execution_stage(strategy_id, 'code_generation_failed')
                    self.mobile.send_status_update_sync(
                        f"❌ **Code Generation Failed** — Strategy #{strategy_id}\n\n"
                        f"Error: {code_result.get('error', 'Unknown')[:500]}\n\n"
                        f"📄 Code plan: `cache/code_plans/strategy_{strategy_id}_plan.md`\n"
                        f"📦 Code cache: `cache/code_changes/strategy_{strategy_id}.json`"
                    )
                    return {
                        'success': False,
                        'error': code_result.get('error', 'Code generation failed')
                    }

                # Cache the generated code for resume capability
                self.code_cache.save(strategy_id, code_result)
                logger.info(f"Cached code changes for strategy {strategy_id}")

                # Notify CEO that code generation is complete
                files_count = len(code_result.get('code_changes', []))
                self.mobile.send_status_update_sync(
                    f"✅ **Code Generation Complete**\n\n"
                    f"Strategy #{strategy_id}\n"
                    f"Generated {files_count} file(s)\n\n"
                    f"Proceeding to build and deploy..."
                )

            # Track stage: pushing branch
            self.db.update_execution_stage(strategy_id, 'pushing_branch')

            # Execute to feature branch (not main)
            logger.info("Deploying to feature branch...")
            result = self.executor.execute_to_preview(
                strategy_id=strategy_id,
                changes=changes,
                code_changes=code_result.get('code_changes', []),
                approval=approval
            )

            if result['success']:
                # Track stage: waiting for merge
                self.db.update_execution_stage(
                    strategy_id,
                    'waiting_merge',
                    metadata={
                        'branch_name': result.get('branch_name'),
                        'preview_url': result.get('preview_url'),
                        'github_url': result.get('github_url'),
                        'vercel_deployment_url': result.get('vercel_deployment_url'),
                        'changes_summary': result.get('changes_summary', '')
                    }
                )
                self.db.update_strategy_status(strategy_id, 'preview')
                logger.info(f"Preview deployed: {result.get('preview_url')}")

                # Notify CEO that code has been pushed
                branch_name = result.get('branch_name', 'unknown')
                github_url = result.get('github_url', '')
                self.mobile.send_status_update_sync(
                    f"🚀 **Code Pushed to GitHub**\n\n"
                    f"Strategy #{strategy_id}\n"
                    f"Branch: `{branch_name}`\n"
                    f"GitHub: {github_url}\n\n"
                    f"Monitoring Vercel build..."
                )

                # Monitor Vercel build status
                logger.info(f"Starting Vercel build monitoring for branch: {branch_name}")
                vercel_result = self.executor.monitor_vercel_build(
                    branch_name=branch_name,
                    poll_interval=60,   # Check every 1 minute
                    max_duration=600    # Max 10 minutes
                )

                # Send notification based on Vercel build result
                vercel_status = vercel_result.get('status', 'unknown')
                elapsed = vercel_result.get('elapsed_seconds', 0)

                if vercel_status == 'ready':
                    deployment_url = vercel_result.get('deployment_url', result.get('preview_url', ''))
                    self.mobile.send_status_update_sync(
                        f"✅ **Vercel Build Succeeded**\n\n"
                        f"Strategy #{strategy_id}\n"
                        f"Build time: {elapsed}s\n"
                        f"Preview: {deployment_url}"
                    )
                elif vercel_status == 'error':
                    build_logs = vercel_result.get('build_logs', '')
                    if build_logs:
                        # Attempt automatic fix-push retry
                        retry_result = self._retry_vercel_build_fix(
                            strategy_id, branch_name, build_logs
                        )
                        if retry_result.get('status') == 'ready':
                            # Update vercel_result with successful retry
                            vercel_result = retry_result
                        else:
                            self.mobile.send_status_update_sync(
                                f"❌ **Vercel Build Failed** (after retries)\n\n"
                                f"Strategy #{strategy_id}\n"
                                f"Branch: `{branch_name}`\n"
                                f"Check Vercel dashboard for details.\n\n"
                                f"📄 Code plan: `cache/code_plans/strategy_{strategy_id}_plan.md`\n"
                                f"📦 Code cache: `cache/code_changes/strategy_{strategy_id}.json`"
                            )
                    else:
                        self.mobile.send_status_update_sync(
                            f"❌ **Vercel Build Failed**\n\n"
                            f"Strategy #{strategy_id}\n"
                            f"Branch: `{branch_name}`\n"
                            f"Could not fetch build logs for auto-fix.\n\n"
                            f"📄 Code plan: `cache/code_plans/strategy_{strategy_id}_plan.md`\n"
                            f"📦 Code cache: `cache/code_changes/strategy_{strategy_id}.json`"
                        )
                elif vercel_status == 'timeout':
                    self.mobile.send_status_update_sync(
                        f"⏱️ **Vercel Build Monitoring Timeout**\n\n"
                        f"Strategy #{strategy_id}\n"
                        f"Build monitoring timed out after {elapsed}s.\n"
                        f"Check Vercel dashboard for status."
                    )
                elif vercel_status != 'skipped':
                    self.mobile.send_status_update_sync(
                        f"⚠️ **Vercel Build Status: {vercel_status}**\n\n"
                        f"Strategy #{strategy_id}"
                    )
            else:
                # Build/push failed - send failure summary to CEO with action buttons
                self.db.update_execution_stage(strategy_id, 'build_failed')

                fixes_applied = result.get('fixes_applied', [])
                build_error = result.get('build_error', result.get('error', 'Unknown error'))
                same_error_count = result.get('same_error_count', 0)

                summary = (
                    f"Build Failed - Strategy #{strategy_id}\n\n"
                    f"Fixes attempted: {len(fixes_applied)}\n"
                )
                if fixes_applied:
                    files_fixed = list(set(f.get('file', 'unknown') for f in fixes_applied))
                    summary += f"Files modified: {', '.join(files_fixed[:10])}\n"
                if same_error_count >= 1:
                    summary += f"Same error repeated: {same_error_count + 1} times\n"
                summary += (
                    f"\nLast error:\n{build_error[:1500]}\n\n"
                    f"📄 Code plan: cache/code_plans/strategy_{strategy_id}_plan.md\n"
                    f"📦 Code cache: cache/code_changes/strategy_{strategy_id}.json\n\n"
                    f"What would you like to do?"
                )

                # Send with action buttons and wait for CEO response
                ceo_action = self._handle_build_failure_decision(
                    strategy_id=strategy_id,
                    summary=summary,
                    branch_name=result.get('branch_name', f'optimize/strategy-{strategy_id}')
                )

                if ceo_action == 'continue':
                    # CEO wants interactive Q&A troubleshooting
                    from troubleshooter import get_troubleshooter
                    ts = get_troubleshooter()
                    qa_result = ts.start_session(
                        failure_type='build_error',
                        error_output=build_error,
                        strategy_id=strategy_id,
                        context={'fixes_applied': len(fixes_applied), 'same_error_count': same_error_count}
                    )

                    if qa_result.get('action') == 'try_fix' and qa_result.get('fixes'):
                        # Apply CEO-guided fixes and retry the entire preview flow
                        app_path = Path(self.executor.gentube_app_path)
                        for fix in qa_result['fixes']:
                            fix_path = fix.get('file_path')
                            fix_content = fix.get('new_content')
                            if fix_path and fix_content:
                                full_path = app_path / fix_path
                                if full_path.exists():
                                    with open(full_path, 'w') as f:
                                        f.write(fix_content)
                                    logger.info(f"Applied CEO-guided fix to: {fix_path}")
                        import git as gitmodule
                        repo = gitmodule.Repo(app_path)
                        repo.git.add(A=True)

                        # Retry build via execute_to_preview
                        self.mobile.send_status_update_sync("Retrying build with CEO-guided fixes...")
                        retry_result = self.executor.execute_to_preview(
                            strategy_id=strategy_id,
                            changes=changes,
                            code_changes=code_result.get('code_changes', []),
                            approval=approval
                        )
                        return retry_result
                    else:
                        # CEO gave up from Q&A
                        self.db.update_strategy_status(strategy_id, 'failed')
                        return result

                elif ceo_action == 'commit_save':
                    # Commit current changes (even with build errors) and save strategy
                    self._commit_and_save(strategy_id, result.get('branch_name'))
                    return {
                        'success': False,
                        'status': 'saved',
                        'message': 'Code committed and strategy saved for later'
                    }

                elif ceo_action == 'save':
                    self.db.update_strategy_status(strategy_id, 'saved')
                    self.mobile.send_status_update_sync(f"Strategy #{strategy_id} saved for later.")
                    return {
                        'success': False,
                        'status': 'saved',
                        'message': 'Strategy saved'
                    }

                elif ceo_action == 'reject':
                    self.db.update_strategy_status(strategy_id, 'rejected')
                    self.mobile.send_status_update_sync(f"Strategy #{strategy_id} rejected.")
                    return {
                        'success': False,
                        'status': 'rejected',
                        'message': 'Strategy rejected'
                    }

                else:
                    # Default: mark as failed (ceo_action == 'fail' or timeout)
                    self.db.update_strategy_status(strategy_id, 'failed')
                    return result

            return result

        except Exception as e:
            logger.error(f"Preview execution failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _handle_build_failure_decision(self, strategy_id: int, summary: str,
                                       branch_name: str) -> str:
        """
        Send failure summary to CEO and wait for their decision.

        Returns:
            Action string: 'save', 'fail', 'reject', 'continue', or 'commit_save'
        """
        # Send summary with inline buttons
        self.mobile.send_build_failure_prompt(strategy_id, summary)

        # Wait for CEO response
        approval = self.db.wait_for_approval(
            strategy_id=strategy_id,
            timeout_minutes=60,  # 1 hour timeout
            fast_poll_interval=10,
            fast_poll_duration=3600,
            response_types=['build_save', 'build_fail', 'build_reject', 'build_continue', 'build_commit_save'],
            require_new=True
        )

        if not approval:
            logger.warning(f"Build failure decision timed out for strategy {strategy_id}")
            self.mobile.send_status_update_sync("Decision timed out. Marking strategy as failed.")
            return 'fail'

        response_type = approval.get('response_type', 'build_fail')
        action = response_type.replace('build_', '')
        logger.info(f"CEO build failure decision for strategy {strategy_id}: {action}")
        return action

    def _commit_and_save(self, strategy_id: int, branch_name: str = None):
        """
        Commit current code changes (even with build errors) and save the strategy.
        Preserves work done by the LLM so it can be returned to later.
        """
        import git
        from pathlib import Path

        app_path = Path(self.executor.gentube_app_path)

        try:
            repo = git.Repo(app_path)
            repo.git.add(A=True)

            # Check if there are changes to commit
            if repo.is_dirty() or repo.untracked_files:
                repo.git.commit(
                    m=f"wip: Partial changes for strategy {strategy_id} (build errors, saved for later)"
                )
                logger.info(f"Committed partial changes for strategy {strategy_id}")

                # Push if we have a branch
                if branch_name:
                    try:
                        repo.git.push('origin', branch_name)
                        logger.info(f"Pushed partial changes to {branch_name}")
                    except Exception as e:
                        logger.warning(f"Could not push partial changes: {e}")
            else:
                logger.info("No changes to commit")

            self.db.update_strategy_status(strategy_id, 'saved')

            # Build GitHub and Vercel links
            github_repo = os.getenv('GITHUB_REPO', '')
            vercel_project = os.getenv('VERCEL_PROJECT_NAME', 'gentube')
            github_url = f"https://github.com/{github_repo}/tree/{branch_name}" if github_repo and branch_name else None
            vercel_url = f"https://vercel.com/{vercel_project}/deployments?branch={branch_name}" if branch_name else None

            msg = f"✅ **Code Committed & Strategy Saved**\n\nStrategy #{strategy_id}\nBranch: `{branch_name or 'N/A'}`\n"
            if github_url:
                msg += f"GitHub: {github_url}\n"
            if vercel_url:
                msg += f"Vercel: {vercel_url}\n"
            msg += f"\nUse /saved to return to this strategy later."
            self.mobile.send_status_update_sync(msg)

        except Exception as e:
            logger.error(f"Failed to commit and save: {e}")
            self.db.update_strategy_status(strategy_id, 'saved')
            self.mobile.send_status_update_sync(
                f"⚠️ Could not commit code ({e}), but strategy #{strategy_id} saved for later.\n"
                f"Use /saved to return to this strategy later."
            )

    def _retry_vercel_build_fix(self, strategy_id: int, branch_name: str,
                                build_logs: str, max_retries: int = 2) -> Dict[str, Any]:
        """
        Attempt to fix Vercel build failures by analyzing logs, applying fixes, and re-pushing.

        Args:
            strategy_id: The strategy being executed
            branch_name: Git branch name
            build_logs: Error logs from Vercel build
            max_retries: Maximum retry attempts

        Returns:
            Final vercel_result dict
        """
        import git
        from pathlib import Path

        app_path = Path(self.executor.gentube_app_path)

        for retry in range(1, max_retries + 1):
            logger.info(f"Vercel build fix retry {retry}/{max_retries} for strategy {strategy_id}")

            self.mobile.send_status_update_sync(
                f"🔧 **Attempting Vercel Build Fix** (retry {retry}/{max_retries})\n\n"
                f"Strategy #{strategy_id}\n"
                f"Analyzing Vercel build logs..."
            )

            # Try installing missing packages first
            installed = self.executor._handle_missing_packages(build_logs)

            # Use LLM to fix code errors from Vercel logs (escalate model on later retries)
            fix_result = self.executor._fix_build_error(build_logs, attempt=retry + 1)

            if not fix_result.get('success') and not installed:
                logger.error(f"Could not generate fix from Vercel logs (retry {retry})")
                continue

            # Apply code fixes if any
            if fix_result.get('success'):
                for file_fix in fix_result.get('fixes', []):
                    file_path = file_fix.get('file_path')
                    new_content = file_fix.get('new_content')
                    if file_path and new_content:
                        full_path = app_path / file_path
                        if full_path.exists():
                            with open(full_path, 'w') as f:
                                f.write(new_content)
                            logger.info(f"Applied Vercel fix to: {file_path}")

            # Commit and push
            try:
                repo = git.Repo(app_path)
                repo.git.add(A=True)
                repo.git.commit(m=f"fix: Vercel build fix retry {retry} for strategy {strategy_id}")
                repo.git.push('origin', branch_name)
                logger.info(f"Pushed Vercel build fix (retry {retry})")
            except Exception as e:
                logger.error(f"Failed to push Vercel fix: {e}")
                continue

            # Re-monitor Vercel build
            vercel_result = self.executor.monitor_vercel_build(
                branch_name=branch_name,
                poll_interval=60,
                max_duration=600
            )

            if vercel_result.get('status') == 'ready':
                self.mobile.send_status_update_sync(
                    f"✅ **Vercel Build Fixed** (retry {retry})\n\n"
                    f"Strategy #{strategy_id}\n"
                    f"Preview: {vercel_result.get('deployment_url', '')}"
                )
                return vercel_result

            # Update build_logs for next retry
            build_logs = vercel_result.get('build_logs', build_logs)

        logger.error(f"Vercel build fix exhausted {max_retries} retries")

        # Trigger interactive troubleshooting with CEO
        from troubleshooter import get_troubleshooter
        ts = get_troubleshooter()
        qa_result = ts.start_session(
            failure_type='vercel_error',
            error_output=build_logs,
            strategy_id=strategy_id,
            context={'branch_name': branch_name, 'retries_exhausted': max_retries}
        )

        if qa_result.get('action') == 'try_fix' and qa_result.get('fixes'):
            # Apply CEO-guided fixes, commit, push, re-monitor
            for fix in qa_result['fixes']:
                fix_path = fix.get('file_path')
                fix_content = fix.get('new_content')
                if fix_path and fix_content:
                    full_path = app_path / fix_path
                    if full_path.exists():
                        with open(full_path, 'w') as f:
                            f.write(fix_content)
                        logger.info(f"Applied CEO-guided Vercel fix to: {fix_path}")

            try:
                repo = git.Repo(app_path)
                repo.git.add(A=True)
                repo.git.commit(m=f"fix: CEO-guided Vercel fix for strategy {strategy_id}")
                repo.git.push('origin', branch_name)
                logger.info("Pushed CEO-guided Vercel fix")

                vercel_result = self.executor.monitor_vercel_build(
                    branch_name=branch_name,
                    poll_interval=60,
                    max_duration=600
                )

                if vercel_result.get('status') == 'ready':
                    self.mobile.send_status_update_sync(
                        f"Vercel Build Fixed (CEO-guided)\n\n"
                        f"Strategy #{strategy_id}\n"
                        f"Preview: {vercel_result.get('deployment_url', '')}"
                    )
                return vercel_result

            except Exception as e:
                logger.error(f"Failed to push CEO-guided Vercel fix: {e}")

        return vercel_result

    def _stage_wait_for_merge(self, strategy_id: int, preview_url: str,
                               branch_name: str, changes_summary: str,
                               github_url: str = None,
                               vercel_deployment_url: str = None) -> Dict[str, Any]:
        """Stage 6: Wait for CEO to approve merge to develop"""
        try:
            # Send preview link to CEO with all URLs
            asyncio.run(self.mobile.send_preview_link(
                strategy_id=strategy_id,
                preview_url=preview_url,
                branch_name=branch_name,
                changes_summary=changes_summary,
                github_url=github_url,
                vercel_deployment_url=vercel_deployment_url
            ))

            # Track timestamp to filter for new approvals after defer
            after_timestamp = None

            # Loop to handle defer responses (keep waiting until merge/discard)
            while True:
                logger.info(f"Waiting for merge approval (timeout: {self.approval_timeout_hours} hours)...")

                # Wait for merge, discard, or defer response
                # Use require_new=True to only get approvals created after we start waiting
                # (or after the last defer response)
                merge_approval = self.db.wait_for_approval(
                    strategy_id,
                    timeout_minutes=self.approval_timeout_hours * 60,
                    poll_interval=300,
                    response_types=['merge', 'discard', 'defer'],
                    require_new=True,
                    after_timestamp=after_timestamp
                )

                if not merge_approval:
                    self.db.update_strategy_status(strategy_id, 'merge_timeout')
                    return {
                        'success': False,
                        'status': 'timeout',
                        'message': 'Merge approval timeout'
                    }

                response_type = merge_approval['response_type']

                if response_type == 'merge':
                    # Execute merge to develop
                    merge_result = self.executor.merge_to_develop(branch_name)

                    if merge_result['success']:
                        self.db.update_strategy_status(strategy_id, 'merged')
                        # Clean up code cache after successful merge
                        self.code_cache.delete(strategy_id)
                        self.mobile.send_status_update_sync(
                            f"✅ Successfully merged {branch_name} to develop!"
                        )
                        return {
                            'success': True,
                            'status': 'merged',
                            'approval': merge_approval
                        }
                    else:
                        self.db.update_strategy_status(strategy_id, 'merge_failed')
                        self.mobile.send_status_update_sync(
                            f"❌ Merge failed: {merge_result.get('error', 'Unknown error')}"
                        )
                        return {
                            'success': False,
                            'status': 'merge_failed',
                            'error': merge_result.get('error')
                        }

                elif response_type == 'discard':
                    # Delete the feature branch
                    discard_result = self.executor.discard_branch(branch_name)
                    self.db.update_strategy_status(strategy_id, 'discarded')
                    # Clean up code cache after discard
                    self.code_cache.delete(strategy_id)
                    self.mobile.send_status_update_sync(
                        f"🗑️ Branch {branch_name} has been deleted."
                    )
                    return {
                        'success': False,
                        'status': 'discarded',
                        'message': 'Preview branch discarded'
                    }

                elif response_type == 'defer':
                    # User deferred - keep preview active, continue waiting
                    logger.info(f"Strategy {strategy_id} deferred, continuing to wait for merge decision...")
                    # Update timestamp to only look for NEW approvals after this defer
                    after_timestamp = merge_approval.get('created_at')
                    # Loop back to wait for another response

                else:
                    return {
                        'success': False,
                        'status': 'unknown',
                        'message': f'Unknown response type: {response_type}'
                    }

        except Exception as e:
            logger.error(f"Merge approval failed: {e}")
            return {
                'success': False,
                'status': 'error',
                'error': str(e)
            }

    def _stage_monitor_deployment(self, cycle_id: str, strategy_id: int,
                                   metrics_before: Dict) -> Dict[str, Any]:
        """Stage 6: Monitor deployment for issues"""
        try:
            # Monitor for a short period (1 hour for MVP)
            monitoring = self.safety.monitor_cycle(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                metrics_before=metrics_before,
                check_interval_minutes=15,
                duration_hours=1
            )

            return monitoring

        except Exception as e:
            logger.error(f"Monitoring failed: {e}")
            return {
                'status': 'error',
                'error': str(e),
                'should_rollback': False
            }

    def _stage_collect_post_metrics(self, strategy_id: int, cycle_id: str) -> Dict[str, Any]:
        """Stage 7: Collect post-deployment metrics"""
        try:
            # Wait longer period for meaningful data (6 hours)
            metrics = self.collector.collect_metrics(
                strategy_id=strategy_id,
                cycle_id=cycle_id,
                wait_hours=6
            )

            return {
                'success': True,
                'data': {'metrics': metrics}
            }

        except Exception as e:
            logger.error(f"Post-metrics collection failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_evaluate_success(self, metrics_before: Dict, metrics_after: Dict,
                                expected_impact: str) -> Dict[str, Any]:
        """Stage 8: Evaluate if strategy was successful"""
        try:
            evaluation = self.strategy_engine.evaluate_strategy_success(
                metrics_before,
                metrics_after,
                expected_impact
            )

            # Send results notification
            if evaluation.get('success'):
                self.mobile.send_status_update_sync(
                    f"✅ Strategy successful! {evaluation.get('actual_impact', '')}"
                )
            else:
                self.mobile.send_status_update_sync(
                    f"⚠️ Strategy below expectations. {evaluation.get('actual_impact', '')}"
                )

            return evaluation

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _finalize_cycle(self, cycle_id: str, cycle_result: Dict) -> None:
        """Finalize cycle and log results"""
        try:
            strategy_id = cycle_result.get('stages', {}).get('strategy_generation', {}).get('strategy_id')

            metrics_before = cycle_result.get('stages', {}).get('data_collection', {}).get('data', {}).get('metrics')
            metrics_after = cycle_result.get('stages', {}).get('post_metrics', {}).get('data', {}).get('metrics')

            success_indicators = cycle_result.get('stages', {}).get('evaluation', {})

            errors = cycle_result.get('error', '')
            if not errors:
                # Collect errors from failed stages
                failed_stages = [
                    stage for stage, result in cycle_result.get('stages', {}).items()
                    if isinstance(result, dict) and not result.get('success', True)
                ]
                if failed_stages:
                    errors = f"Failed stages: {', '.join(failed_stages)}"

            # Complete cycle in database
            self.db.complete_cycle(
                cycle_id=cycle_id,
                status=cycle_result['status'],
                strategy_id=strategy_id,
                metrics_after=metrics_after,
                success_indicators=success_indicators,
                errors=errors
            )

            logger.info(f"Cycle {cycle_id} finalized in database")

        except Exception as e:
            logger.error(f"Cycle finalization failed: {e}")

    # ============ SCHEDULING ============

    def run_continuous(self, end_time: Optional[datetime] = None):
        """
        Run optimization loop continuously on schedule

        Args:
            end_time: Optional datetime to stop running (None = run forever)
        """
        if end_time:
            logger.info(f"Starting continuous optimization loop (interval: {self.cycle_interval_hours}h, until: {end_time})")
        else:
            logger.info(f"Starting continuous optimization loop (interval: {self.cycle_interval_hours}h, running forever)")

        self.is_running = True

        # Schedule cycle
        schedule.every(self.cycle_interval_hours).hours.do(self.run_cycle)

        # Run first cycle immediately
        self.run_cycle()

        # Keep running until stopped or end_time reached
        while self.is_running:
            # Check if we've reached the end time
            if end_time and datetime.utcnow() >= end_time:
                logger.info(f"Reached scheduled end time ({end_time}), stopping...")
                break

            # Check if CEO initiated an action via /restart between cycles
            chat_id = int(os.getenv('TELEGRAM_CHAT_ID', '0'))
            if chat_id:
                selection = self.mobile.get_menu_selection(chat_id)
                if selection and (selection.startswith('execute:') or selection == 'restart'):
                    logger.info(f"CEO initiated action between cycles: {selection}")
                    self.mobile.send_status_update_sync(f"🔄 **Starting new cycle** (CEO action: {selection})")
                    self.run_cycle()
                    continue

            schedule.run_pending()
            time.sleep(5)  # Check every 5 seconds for CEO actions

        logger.info("Continuous optimization loop stopped")

    def stop(self):
        """Stop the continuous loop"""
        logger.info("Stopping optimization loop...")
        self.is_running = False


# Singleton instance
_loop_controller = None

def get_loop_controller() -> LoopController:
    """Get or create loop controller singleton"""
    global _loop_controller
    if _loop_controller is None:
        _loop_controller = LoopController()
    return _loop_controller
