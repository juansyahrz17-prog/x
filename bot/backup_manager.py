"""
Automatic GitHub Backup Manager for VoraHub Bot
Handles automatic commits and pushes of JSON data files to GitHub
"""

import os
import subprocess
import threading
from datetime import datetime
from typing import List, Optional
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class GitBackupManager:
    """Manages automatic Git backups to GitHub"""
    
    def __init__(self, repo_path: str, remote_url: Optional[str] = None, auth_token: Optional[str] = None):
        """
        Initialize the Git backup manager
        
        Args:
            repo_path: Path to the repository (bot directory)
            remote_url: GitHub repository URL (optional, can be set later)
            auth_token: GitHub Personal Access Token for authentication (optional)
        """
        self.repo_path = repo_path
        self.remote_url = remote_url
        self.auth_token = auth_token
        self.git_initialized = False
        
        # Initialize Git if needed
        self._init_git()
    
    def _run_git_command(self, command: List[str]) -> tuple[bool, str]:
        """
        Run a git command and return success status and output
        
        Args:
            command: Git command as list of strings
            
        Returns:
            Tuple of (success: bool, output: str)
        """
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                return True, result.stdout
            else:
                logger.warning(f"Git command failed: {' '.join(command)}\nError: {result.stderr}")
                return False, result.stderr
                
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out: {' '.join(command)}")
            return False, "Command timed out"
        except Exception as e:
            logger.error(f"Error running git command: {e}")
            return False, str(e)
    
    def _init_git(self):
        """Initialize Git repository if not already initialized"""
        git_dir = os.path.join(self.repo_path, ".git")
        
        if not os.path.exists(git_dir):
            logger.info("Initializing Git repository...")
            success, output = self._run_git_command(["git", "init"])
            
            if success:
                logger.info("Git repository initialized successfully")
                
                # Configure git user
                self._run_git_command(["git", "config", "user.name", "VoraHub Bot"])
                self._run_git_command(["git", "config", "user.email", "bot@vorahub.local"])
                
                self.git_initialized = True
            else:
                logger.error(f"Failed to initialize Git repository: {output}")
                return
        else:
            self.git_initialized = True
            logger.info("Git repository already initialized")
    
    def set_remote(self, remote_url: str, auth_token: Optional[str] = None):
        """
        Set or update the GitHub remote repository
        
        Args:
            remote_url: GitHub repository URL
            auth_token: Personal Access Token for authentication
        """
        self.remote_url = remote_url
        self.auth_token = auth_token
        
        if not self.git_initialized:
            logger.error("Git not initialized, cannot set remote")
            return False
        
        # Build authenticated URL if token provided
        if auth_token and "github.com" in remote_url:
            # Convert https://github.com/user/repo.git to https://token@github.com/user/repo.git
            if remote_url.startswith("https://"):
                remote_url = remote_url.replace("https://", f"https://{auth_token}@")
        
        # Check if remote exists
        success, output = self._run_git_command(["git", "remote", "get-url", "origin"])
        
        if success:
            # Update existing remote
            logger.info("Updating existing remote origin...")
            success, output = self._run_git_command(["git", "remote", "set-url", "origin", remote_url])
        else:
            # Add new remote
            logger.info("Adding new remote origin...")
            success, output = self._run_git_command(["git", "remote", "add", "origin", remote_url])
        
        if success:
            logger.info("Remote repository configured successfully")
            return True
        else:
            logger.error(f"Failed to set remote: {output}")
            return False
    
    def backup_files(self, files: List[str], commit_message: Optional[str] = None) -> bool:
        """
        Commit and push specified files to GitHub
        
        Args:
            files: List of filenames to backup (relative to repo_path)
            commit_message: Custom commit message (optional)
            
        Returns:
            True if backup successful, False otherwise
        """
        if not self.git_initialized:
            logger.error("Git not initialized, cannot backup")
            return False
        
        if not files:
            logger.warning("No files specified for backup")
            return False
        
        # Generate commit message if not provided
        if not commit_message:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            files_str = ", ".join(files)
            commit_message = f"Auto-backup: {files_str} - {timestamp}"
        
        try:
            # Stage files
            for file in files:
                file_path = os.path.join(self.repo_path, file)
                if os.path.exists(file_path):
                    success, output = self._run_git_command(["git", "add", file])
                    if not success:
                        logger.error(f"Failed to stage {file}: {output}")
                        return False
                else:
                    logger.warning(f"File not found: {file_path}")
            
            # Check if there are changes to commit
            success, output = self._run_git_command(["git", "diff", "--cached", "--quiet"])
            if success:
                logger.info("No changes to commit")
                return True  # Not an error, just nothing to do
            
            # Commit changes
            success, output = self._run_git_command(["git", "commit", "-m", commit_message])
            if not success:
                logger.error(f"Failed to commit: {output}")
                return False
            
            logger.info(f"Committed changes: {commit_message}")
            
            # Push to remote if configured
            if self.remote_url:
                success, output = self._run_git_command(["git", "push", "origin", "master"])
                if not success:
                    # Try 'main' branch if 'master' fails
                    success, output = self._run_git_command(["git", "push", "origin", "main"])
                    
                if success:
                    logger.info("Successfully pushed to GitHub")
                    return True
                else:
                    logger.error(f"Failed to push to GitHub: {output}")
                    return False
            else:
                logger.warning("No remote configured, skipping push")
                return True
                
        except Exception as e:
            logger.error(f"Error during backup: {e}")
            return False
    
    def async_backup(self, files: List[str], commit_message: Optional[str] = None):
        """
        Perform backup in a background thread to avoid blocking
        
        Args:
            files: List of filenames to backup
            commit_message: Custom commit message (optional)
        """
        def backup_thread():
            self.backup_files(files, commit_message)
        
        thread = threading.Thread(target=backup_thread, daemon=True)
        thread.start()
        logger.info(f"Started async backup for: {', '.join(files)}")


# Global backup manager instance
_backup_manager: Optional[GitBackupManager] = None

def init_backup_manager(repo_path: str, remote_url: Optional[str] = None, auth_token: Optional[str] = None):
    """
    Initialize the global backup manager
    
    Args:
        repo_path: Path to the bot directory
        remote_url: GitHub repository URL (optional)
        auth_token: GitHub Personal Access Token (optional)
    """
    global _backup_manager
    _backup_manager = GitBackupManager(repo_path, remote_url, auth_token)
    logger.info("Backup manager initialized")
    return _backup_manager

def get_backup_manager() -> Optional[GitBackupManager]:
    """Get the global backup manager instance"""
    return _backup_manager

def backup_to_github(files: List[str], commit_message: Optional[str] = None, async_mode: bool = True):
    """
    Convenience function to backup files to GitHub
    
    Args:
        files: List of filenames to backup
        commit_message: Custom commit message (optional)
        async_mode: If True, backup runs in background thread (default: True)
    """
    if _backup_manager is None:
        logger.warning("Backup manager not initialized, skipping backup")
        return
    
    if async_mode:
        _backup_manager.async_backup(files, commit_message)
    else:
        _backup_manager.backup_files(files, commit_message)
