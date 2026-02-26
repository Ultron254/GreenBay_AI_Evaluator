#!/usr/bin/env python3
"""
Database management script for GreenBay Market chatbot.
Provides commands for database operations using Alembic.
"""

import subprocess
import sys
import os
from pathlib import Path

def run_command(command, description):
    """Run a command and handle errors."""
    print(f"🔄 {description}...")
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        print(f"✅ {description} completed successfully")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed")
        print(f"Error: {e.stderr}")
        return False

def main():
    """Main function to handle database operations."""
    if len(sys.argv) < 2:
        print("Usage: python manage_db.py <command>")
        print("Commands:")
        print("  init          - Initialize database (create tables)")
        print("  upgrade       - Apply all pending migrations")
        print("  downgrade     - Rollback one migration")
        print("  revision      - Create new migration")
        print("  current       - Show current migration")
        print("  history       - Show migration history")
        print("  reset         - Reset database (drop and recreate)")
        print("  status        - Show database status")
        return
    
    command = sys.argv[1]
    
    # Change to project directory
    project_dir = Path(__file__).parent
    os.chdir(project_dir)
    
    # Activate virtual environment
    venv_python = project_dir / "venv" / "bin" / "python"
    venv_alembic = project_dir / "venv" / "bin" / "alembic"
    
    if not venv_python.exists():
        print("❌ Virtual environment not found. Please run: python -m venv venv")
        return
    
    if command == "init":
        print("🚀 Initializing GreenBay Market chatbot database...")
        print("=" * 60)
        
        # Check if tables exist
        result = subprocess.run(
            f"{venv_python} -c \"from app.database.db import engine; from sqlalchemy import text; "
            f"with engine.connect() as conn: result = conn.execute(text('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = \\'public\\'')); "
            f"print('Tables:', result.scalar())\"",
            shell=True, capture_output=True, text=True
        )
        
        if "Tables: 0" in result.stdout:
            print("📊 No tables found. Creating initial migration...")
            run_command(f"{venv_alembic} revision --autogenerate -m 'Initial migration'", "Creating migration")
            run_command(f"{venv_alembic} upgrade head", "Applying migration")
        else:
            print("📊 Tables already exist. Checking migration status...")
            run_command(f"{venv_alembic} current", "Current migration status")
    
    elif command == "upgrade":
        run_command(f"{venv_alembic} upgrade head", "Upgrading database")
    
    elif command == "downgrade":
        run_command(f"{venv_alembic} downgrade -1", "Downgrading database")
    
    elif command == "revision":
        if len(sys.argv) < 3:
            message = input("Enter migration message: ")
        else:
            message = sys.argv[2]
        run_command(f"{venv_alembic} revision --autogenerate -m '{message}'", f"Creating migration: {message}")
    
    elif command == "current":
        run_command(f"{venv_alembic} current", "Current migration")
    
    elif command == "history":
        run_command(f"{venv_alembic} history", "Migration history")
    
    elif command == "reset":
        print("⚠️  WARNING: This will DROP ALL TABLES and recreate them!")
        confirm = input("Are you sure? Type 'yes' to continue: ")
        if confirm.lower() == 'yes':
            print("🔄 Resetting database...")
            run_command(f"{venv_alembic} downgrade base", "Dropping all tables")
            run_command(f"{venv_alembic} upgrade head", "Recreating tables")
        else:
            print("❌ Reset cancelled")
    
    elif command == "status":
        print("📊 GreenBay Market Chatbot Database Status")
        print("=" * 50)
        
        # Check database connection
        result = subprocess.run(
            f"{venv_python} -c \"from app.config import get_settings; print('Database URL:', get_settings().database_url)\"",
            shell=True, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ Database URL: {result.stdout.strip()}")
        else:
            print(f"❌ Database configuration error: {result.stderr}")
            return
        
        # Check Alembic status
        run_command(f"{venv_alembic} current", "Current migration")
        
        # Check table count
        result = subprocess.run(
            f"{venv_python} -c \"from app.database.db import engine; from sqlalchemy import text; "
            f"with engine.connect() as conn: result = conn.execute(text('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = \\'public\\' AND table_name != \\'alembic_version\\'')); "
            f"print('Tables created:', result.scalar())\"",
            shell=True, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"📊 {result.stdout.strip()}")
        
        # List tables
        result = subprocess.run(
            f"{venv_python} -c \"from app.database.db import engine; from sqlalchemy import text; "
            f"with engine.connect() as conn: result = conn.execute(text('SELECT table_name FROM information_schema.tables WHERE table_schema = \\'public\\' AND table_name != \\'alembic_version\\' ORDER BY table_name')); "
            f"tables = [row[0] for row in result]; print('\\n'.join(tables))\"",
            shell=True, capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            print("📋 Tables:")
            for table in result.stdout.strip().split('\n'):
                print(f"   • {table}")
    
    else:
        print(f"❌ Unknown command: {command}")
        print("Available commands: init, upgrade, downgrade, revision, current, history, reset, status")

if __name__ == "__main__":
    main()
