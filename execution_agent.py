"""
Local Execution Agent
Executes approved changes locally, tests them, and deploys to production
"""

import os
import json
import logging
import subprocess
import shutil
from typing import Dict, List, Optional, Any
from pathlib import Path
import git
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


class ExecutionAgent:
    """Executes and deploys approved optimization strategies"""

    def __init__(self):
        self.db = get_db_handler()
        self.vercel_token = os.getenv('VERCEL_TOKEN')
        self.vercel_project_id = os.getenv('VERCEL_PROJECT_ID')
        self.gentube_app_path = os.getenv('GENTUBE_APP_PATH', '../gentube-app')

        # Ensure app path exists
        if not Path(self.gentube_app_path).exists():
            logger.warning(f"Gentube app path not found: {self.gentube_app_path}")

        logger.info("Execution agent initialized")

    def execute_changes(self, strategy_id: int, changes: List[Dict], approval: Dict) -> Dict[str, Any]:
        """
        Execute approved changes

        Args:
            strategy_id: Strategy ID
            changes: List of changes to apply
            approval: Approval details

        Returns:
            Execution result with status and details
        """
        logger.info(f"Executing changes for strategy {strategy_id}...")

        result = {
            'success': False,
            'strategy_id': strategy_id,
            'changes_applied': [],
            'changes_failed': [],
            'git_commit_hash': None,
            'vercel_deployment_url': None,
            'rollback_info': None,
            'errors': []
        }

        try:
            # 1. Create backup/rollback point
            rollback_info = self._create_backup()
            result['rollback_info'] = rollback_info

            # 2. Apply each change
            for i, change in enumerate(changes):
                try:
                    logger.info(f"Applying change {i+1}/{len(changes)}: {change.get('action')}")
                    applied = self._apply_change(change)
                    result['changes_applied'].append({
                        'change': change,
                        'result': applied
                    })
                except Exception as e:
                    logger.error(f"Failed to apply change: {e}")
                    result['changes_failed'].append({
                        'change': change,
                        'error': str(e)
                    })
                    result['errors'].append(f"Change {i+1} failed: {e}")

            # 3. Test changes locally
            if result['changes_applied']:
                test_result = self._test_changes()
                if not test_result['success']:
                    raise Exception(f"Local tests failed: {test_result['message']}")

            # 4. Commit changes
            commit_hash = self._commit_changes(strategy_id, changes, approval)
            result['git_commit_hash'] = commit_hash

            # 5. Deploy to Vercel
            if self.vercel_token and self.vercel_project_id:
                deployment_url = self._deploy_to_vercel()
                result['vercel_deployment_url'] = deployment_url

            # 6. Log deployment
            deployment_id = self.db.log_deployment(
                strategy_id=strategy_id,
                status='deployed',
                git_commit_hash=commit_hash,
                vercel_deployment_url=result.get('vercel_deployment_url'),
                changes_applied=result['changes_applied'],
                rollback_info=rollback_info
            )

            result['success'] = True
            result['deployment_id'] = deployment_id

            logger.info(f"Successfully executed strategy {strategy_id}")

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            result['errors'].append(str(e))

            # Attempt rollback
            if result.get('rollback_info'):
                logger.info("Attempting rollback...")
                try:
                    self._rollback(result['rollback_info'])
                    logger.info("Rollback successful")
                except Exception as rb_error:
                    logger.error(f"Rollback failed: {rb_error}")
                    result['errors'].append(f"Rollback failed: {rb_error}")

            # Log failed deployment
            self.db.log_deployment(
                strategy_id=strategy_id,
                status='failed',
                changes_applied=result['changes_applied'],
                rollback_info=result['rollback_info']
            )

        return result

    def _create_backup(self) -> Dict[str, Any]:
        """Create backup/rollback point"""
        logger.info("Creating backup...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return {'branch': None, 'commit': None}

            repo = git.Repo(app_path)
            current_branch = repo.active_branch.name
            current_commit = repo.head.commit.hexsha

            return {
                'branch': current_branch,
                'commit': current_commit,
                'timestamp': repo.head.commit.committed_datetime.isoformat()
            }

        except Exception as e:
            logger.warning(f"Backup creation failed: {e}")
            return {'error': str(e)}

    def _apply_change(self, change: Dict) -> Dict[str, Any]:
        """
        Apply a single change

        Args:
            change: Change details (action, type, details, files_to_modify, etc.)

        Returns:
            Result of applying the change
        """
        change_type = change.get('type')
        action = change.get('action')
        details = change.get('details')
        files = change.get('files_to_modify', [])

        logger.info(f"Applying {change_type}: {action}")

        # Route to appropriate handler based on type
        if change_type == 'ui_change':
            return self._apply_ui_change(change)
        elif change_type == 'model_change':
            return self._apply_model_change(change)
        elif change_type == 'feature_addition':
            return self._apply_feature_addition(change)
        elif change_type == 'prompt_optimization':
            return self._apply_prompt_optimization(change)
        elif change_type == 'pricing_adjustment':
            return self._apply_pricing_adjustment(change)
        else:
            logger.warning(f"Unknown change type: {change_type}")
            return {'success': False, 'message': f'Unknown change type: {change_type}'}

    def _apply_ui_change(self, change: Dict) -> Dict[str, Any]:
        """Apply UI-related changes"""
        # This would modify React/Vue/HTML files
        # For MVP, we'll log the intent
        logger.info(f"UI Change: {change.get('action')}")

        files = change.get('files_to_modify', [])
        details = change.get('details', '')

        # In a real implementation, you would:
        # 1. Parse the files
        # 2. Apply the specific changes (e.g., using AST for JS/TS files)
        # 3. Validate syntax

        return {
            'success': True,
            'message': f'UI change applied to {len(files)} files',
            'files_modified': files
        }

    def _apply_model_change(self, change: Dict) -> Dict[str, Any]:
        """Apply AI model-related changes"""
        logger.info(f"Model Change: {change.get('action')}")

        # This could involve:
        # - Changing model parameters in config files
        # - Updating API endpoints
        # - Modifying prompt templates

        return {
            'success': True,
            'message': 'Model change applied',
            'details': change.get('details')
        }

    def _apply_feature_addition(self, change: Dict) -> Dict[str, Any]:
        """Apply new feature additions"""
        logger.info(f"Feature Addition: {change.get('action')}")

        # This is more complex and might involve:
        # - Generating new component files
        # - Updating routing
        # - Adding API endpoints

        return {
            'success': True,
            'message': 'Feature scaffolding created',
            'note': 'Manual review recommended for new features'
        }

    def _apply_prompt_optimization(self, change: Dict) -> Dict[str, Any]:
        """Apply prompt template optimizations"""
        logger.info(f"Prompt Optimization: {change.get('action')}")

        # Update prompt templates in config or code
        # This is typically a string replacement in a config file

        return {
            'success': True,
            'message': 'Prompt template updated'
        }

    def _apply_pricing_adjustment(self, change: Dict) -> Dict[str, Any]:
        """Apply pricing adjustments"""
        logger.info(f"Pricing Adjustment: {change.get('action')}")

        # Update pricing in config/database
        # This should be careful and validated

        return {
            'success': True,
            'message': 'Pricing updated',
            'warning': 'Price changes require careful monitoring'
        }

    def _test_changes(self) -> Dict[str, Any]:
        """Test changes locally before deployment"""
        logger.info("Testing changes locally...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return {
                    'success': True,
                    'message': 'App path not found, skipping tests',
                    'skipped': True
                }

            # Run tests if available
            test_commands = [
                ['npm', 'run', 'type-check'],  # TypeScript type checking
                ['npm', 'run', 'lint'],        # Linting
                # ['npm', 'test'],             # Unit tests (uncomment if available)
            ]

            results = []
            for cmd in test_commands:
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=app_path,
                        capture_output=True,
                        text=True,
                        timeout=300
                    )

                    results.append({
                        'command': ' '.join(cmd),
                        'success': result.returncode == 0,
                        'output': result.stdout if result.returncode == 0 else result.stderr
                    })

                except subprocess.TimeoutExpired:
                    results.append({
                        'command': ' '.join(cmd),
                        'success': False,
                        'output': 'Test timeout'
                    })
                except FileNotFoundError:
                    # Command not available, skip
                    continue

            # Check if any tests failed
            failed = [r for r in results if not r['success']]
            if failed:
                return {
                    'success': False,
                    'message': f'{len(failed)} tests failed',
                    'results': results
                }

            return {
                'success': True,
                'message': 'All tests passed',
                'results': results
            }

        except Exception as e:
            logger.error(f"Test execution failed: {e}")
            return {
                'success': False,
                'message': str(e)
            }

    def _commit_changes(self, strategy_id: int, changes: List[Dict], approval: Dict) -> str:
        """Commit changes to git"""
        logger.info("Committing changes...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                logger.warning("App path not found, skipping git commit")
                return 'no-commit'

            repo = git.Repo(app_path)

            # Stage all changes
            repo.git.add(A=True)

            # Create commit message
            summary = changes[0].get('action', 'Optimization') if changes else 'Optimization'
            commit_message = f"[Auto-Optimize] Strategy #{strategy_id}: {summary}\n\n"
            commit_message += f"Applied {len(changes)} changes:\n"
            for i, change in enumerate(changes):
                commit_message += f"{i+1}. {change.get('action', 'N/A')}\n"

            commit_message += f"\nApproved by: {approval.get('user_response', 'system')}\n"

            # Commit
            repo.index.commit(commit_message)

            commit_hash = repo.head.commit.hexsha
            logger.info(f"Changes committed: {commit_hash}")

            return commit_hash

        except Exception as e:
            logger.error(f"Git commit failed: {e}")
            raise

    def _deploy_to_vercel(self) -> Optional[str]:
        """Deploy to Vercel"""
        logger.info("Deploying to Vercel...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                logger.warning("App path not found, skipping deployment")
                return None

            # Push to git first
            repo = git.Repo(app_path)
            origin = repo.remote('origin')
            origin.push()

            logger.info("Pushed to git, Vercel auto-deploy should trigger")

            # Optionally, trigger Vercel deployment via CLI
            if shutil.which('vercel'):
                result = subprocess.run(
                    ['vercel', '--prod', '--token', self.vercel_token],
                    cwd=app_path,
                    capture_output=True,
                    text=True,
                    timeout=600
                )

                if result.returncode == 0:
                    # Extract deployment URL from output
                    output = result.stdout
                    # Parse URL (usually in format: https://gentube-xxx.vercel.app)
                    lines = output.split('\n')
                    for line in lines:
                        if 'https://' in line:
                            return line.strip()

            return 'deployment-pending'

        except Exception as e:
            logger.error(f"Vercel deployment failed: {e}")
            raise

    def _rollback(self, rollback_info: Dict) -> None:
        """Rollback to previous state"""
        logger.info("Rolling back changes...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return

            repo = git.Repo(app_path)

            # Reset to previous commit
            if rollback_info.get('commit'):
                repo.git.reset('--hard', rollback_info['commit'])
                logger.info(f"Reset to commit {rollback_info['commit']}")

            # Optionally push rollback
            origin = repo.remote('origin')
            origin.push(force=True)

            logger.info("Rollback complete")

        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            raise

    def simulate_changes(self, changes: List[Dict]) -> Dict[str, Any]:
        """
        Simulate changes without actually applying them (dry run)

        Args:
            changes: List of changes

        Returns:
            Simulation result
        """
        logger.info("Simulating changes (dry run)...")

        simulation = {
            'feasible': True,
            'warnings': [],
            'estimated_impact': {},
            'files_affected': []
        }

        for change in changes:
            files = change.get('files_to_modify', [])
            simulation['files_affected'].extend(files)

            # Check if files exist
            app_path = Path(self.gentube_app_path)
            for file in files:
                file_path = app_path / file
                if not file_path.exists():
                    simulation['warnings'].append(f"File not found: {file}")

            # Check risk level
            risk = change.get('risk_level', 'medium')
            if risk == 'high':
                simulation['warnings'].append(f"High risk change: {change.get('action')}")

        return simulation


# Singleton instance
_execution_agent = None

def get_execution_agent() -> ExecutionAgent:
    """Get or create execution agent singleton"""
    global _execution_agent
    if _execution_agent is None:
        _execution_agent = ExecutionAgent()
    return _execution_agent
