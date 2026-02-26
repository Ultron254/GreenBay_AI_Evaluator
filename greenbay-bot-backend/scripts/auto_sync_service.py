#!/usr/bin/env python3
"""
Auto-sync service for Shopify products.
Runs sync_shopify_products.py on a schedule.
"""
import os
import sys
import time
import json
import schedule
import subprocess
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYNC_SCRIPT = os.path.join(os.path.dirname(__file__), "sync_shopify_products.py")
SYNC_LOG_FILE = os.path.join(os.path.dirname(__file__), "sync_history.json")


def load_sync_history():
    """
    Load sync history from JSON file.
    
    Returns:
        Dictionary with sync history data
    """
    if not os.path.exists(SYNC_LOG_FILE):
        return {
            "service_started": datetime.now().isoformat(),
            "last_sync": None,
            "total_syncs": 0,
            "successful_syncs": 0,
            "failed_syncs": 0,
            "sync_history": []
        }
    
    try:
        with open(SYNC_LOG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Warning: Could not load sync history: {e}")
        return {
            "service_started": datetime.now().isoformat(),
            "last_sync": None,
            "total_syncs": 0,
            "successful_syncs": 0,
            "failed_syncs": 0,
            "sync_history": []
        }


def save_sync_history(data):
    """
    Save sync history to JSON file.
    
    Args:
        data: Dictionary with sync history data
    """
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(SYNC_LOG_FILE), exist_ok=True)
        
        with open(SYNC_LOG_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"💾 Sync history saved to: {SYNC_LOG_FILE}")
    except Exception as e:
        print(f"❌ Error saving sync history: {e}")


def add_sync_record(success, duration, error_message=None):
    """
    Add a sync record to history.
    
    Args:
        success: Whether sync was successful
        duration: Duration in seconds
        error_message: Error message if failed
    """
    try:
        history = load_sync_history()
        
        # Update counters
        history["total_syncs"] += 1
        if success:
            history["successful_syncs"] += 1
        else:
            history["failed_syncs"] += 1
        
        history["last_sync"] = datetime.now().isoformat()
        
        # Add to history (keep last 100 records)
        sync_record = {
            "timestamp": datetime.now().isoformat(),
            "success": success,
            "duration_seconds": round(duration, 1),
            "duration_minutes": round(duration / 60, 1),
        }
        
        if error_message:
            sync_record["error"] = error_message
        
        history["sync_history"].append(sync_record)
        
        # Keep only last 100 records
        if len(history["sync_history"]) > 100:
            history["sync_history"] = history["sync_history"][-100:]
        
        # Save to file
        save_sync_history(history)
        
    except Exception as e:
        print(f"⚠️  Warning: Could not save sync record: {e}")


def run_sync():
    """Run the sync script."""
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🔄 Starting scheduled sync at {start_time.isoformat()}")
    print(f"{'='*60}")
    print(f"📍 Sync script: {SYNC_SCRIPT}")
    print(f"⏱️  Timeout: 30 minutes")
    print(f"{'='*60}\n")
    
    try:
        # Run sync with real-time output
        process = subprocess.Popen(
            [sys.executable, SYNC_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Print output in real-time
        for line in process.stdout:
            print(line, end='', flush=True)
        
        # Wait for process to complete with timeout
        process.wait(timeout=1800)  # 30 minute timeout
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        if process.returncode == 0:
            print(f"\n{'='*60}")
            print(f"✅ Sync completed successfully!")
            print(f"⏱️  Duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")
            print(f"🕐 Finished at: {end_time.isoformat()}")
            print(f"{'='*60}")
            add_sync_record(success=True, duration=duration)
        else:
            print(f"\n{'='*60}")
            print(f"❌ Sync failed with return code {process.returncode}")
            print(f"⏱️  Duration: {duration:.1f} seconds")
            print(f"{'='*60}")
            add_sync_record(success=False, duration=duration, error_message=f"Exit code {process.returncode}")
    
    except subprocess.TimeoutExpired:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        print(f"\n{'='*60}")
        print(f"❌ Sync timeout (exceeded 30 minutes)")
        print(f"⏱️  Duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")
        print(f"💡 Try running manually: python {SYNC_SCRIPT}")
        print(f"{'='*60}")
        if 'process' in locals():
            process.kill()
        add_sync_record(success=False, duration=duration, error_message="Timeout (exceeded 30 minutes)")
    except Exception as e:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        print(f"\n{'='*60}")
        print(f"❌ Error running sync: {e}")
        print(f"⏱️  Duration: {duration:.1f} seconds")
        print(f"{'='*60}")
        import traceback
        traceback.print_exc()
        add_sync_record(success=False, duration=duration, error_message=str(e))
    
    print()


def display_sync_statistics():
    """Display sync statistics from history."""
    try:
        history = load_sync_history()
        
        print("\n📊 Sync Statistics:")
        print("=" * 60)
        print(f"  Service started: {history.get('service_started', 'Unknown')}")
        print(f"  Last sync: {history.get('last_sync', 'Never')}")
        print(f"  Total syncs: {history.get('total_syncs', 0)}")
        print(f"  Successful: {history.get('successful_syncs', 0)}")
        print(f"  Failed: {history.get('failed_syncs', 0)}")
        
        if history.get('total_syncs', 0) > 0:
            success_rate = (history.get('successful_syncs', 0) / history.get('total_syncs', 1)) * 100
            print(f"  Success rate: {success_rate:.1f}%")
        
        # Show last 3 sync records
        if history.get('sync_history'):
            print(f"\n  Last 3 syncs:")
            for record in history['sync_history'][-3:]:
                status = "✅" if record.get('success') else "❌"
                timestamp = record.get('timestamp', 'Unknown')
                duration = record.get('duration_minutes', 0)
                print(f"    {status} {timestamp} ({duration:.1f} min)")
                if not record.get('success') and record.get('error'):
                    print(f"       Error: {record.get('error')}")
        
        print(f"\n  Log file: {SYNC_LOG_FILE}")
        print("=" * 60)
        
    except Exception as e:
        print(f"⚠️  Could not display statistics: {e}")


def main():
    """Main service loop."""
    print("🚀 Starting Shopify Auto-Sync Service")
    print("=" * 60)
    
    # Display sync statistics
    display_sync_statistics()
    
    # Schedule sync every 6 hours
    schedule.every(6).hours.do(run_sync)
    
    # Also schedule a daily sync at 2 AM
    schedule.every().day.at("02:00").do(run_sync)
    
    print("\n📅 Sync schedule:")
    print("  - Every 6 hours")
    print("  - Daily at 2:00 AM")
    print("=" * 60)
    
    # Run initial sync
    print("\n🔄 Running initial sync...")
    run_sync()
    
    # Keep running
    print("\n✅ Service running. Press Ctrl+C to stop.\n")
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Auto-sync service stopped by user")
        sys.exit(0)
