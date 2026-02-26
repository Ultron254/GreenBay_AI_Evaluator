"""LangSmith monitoring dashboard for GreenBay Market chatbot."""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from loguru import logger

from langsmith import Client
from app.config import get_settings

settings = get_settings()


class LangSmithMonitor:
    """LangSmith monitoring client for conversation analytics."""
    
    def __init__(self):
        """Initialize LangSmith monitoring client."""
        if settings.langsmith_api_key:
            self.client = Client(
                api_url=settings.langsmith_endpoint,
                api_key=settings.langsmith_api_key
            )
            self.project = settings.langsmith_project
        else:
            self.client = None
            self.project = None
            logger.warning("LangSmith API key not configured")
    
    def get_conversation_metrics(self, hours: int = 24) -> Dict[str, Any]:
        """Get conversation metrics for the last N hours."""
        if not self.client:
            return {"error": "LangSmith not configured"}
        
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours)
            
            # Get runs from LangSmith
            runs = list(self.client.list_runs(
                project_name=self.project,
                start_time=start_time,
                end_time=end_time,
                limit=1000
            ))
            
            if not runs:
                return {
                    "total_conversations": 0,
                    "success_rate": 0,
                    "average_response_time": 0,
                    "tool_usage_stats": {},
                    "error_rate": 0,
                    "period_hours": hours
                }
            
            # Calculate metrics
            total_conversations = len(runs)
            successful_runs = [r for r in runs if r.status == "success"]
            error_runs = [r for r in runs if r.status == "error"]
            
            success_rate = len(successful_runs) / total_conversations if total_conversations > 0 else 0
            error_rate = len(error_runs) / total_conversations if total_conversations > 0 else 0
            
            # Calculate average response time
            response_times = []
            for run in successful_runs:
                if run.end_time and run.start_time:
                    duration = (run.end_time - run.start_time).total_seconds()
                    response_times.append(duration)
            
            avg_response_time = sum(response_times) / len(response_times) if response_times else 0
            
            # Tool usage statistics
            tool_usage = {}
            for run in runs:
                if hasattr(run, 'extra') and run.extra and 'metadata' in run.extra:
                    metadata = run.extra['metadata']
                    if 'tool_name' in metadata:
                        tool_name = metadata['tool_name']
                        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
            
            return {
                "total_conversations": total_conversations,
                "success_rate": round(success_rate * 100, 2),
                "error_rate": round(error_rate * 100, 2),
                "average_response_time": round(avg_response_time, 2),
                "tool_usage_stats": tool_usage,
                "period_hours": hours,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting conversation metrics: {e}")
            return {"error": str(e)}
    
    def get_tool_performance(self, hours: int = 24) -> Dict[str, Any]:
        """Get tool performance metrics."""
        if not self.client:
            return {"error": "LangSmith not configured"}
        
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours)
            
            runs = list(self.client.list_runs(
                project_name=self.project,
                start_time=start_time,
                end_time=end_time,
                limit=1000
            ))
            
            tool_stats = {}
            
            for run in runs:
                if hasattr(run, 'name') and run.name:
                    tool_name = run.name
                    if tool_name not in tool_stats:
                        tool_stats[tool_name] = {
                            "total_calls": 0,
                            "successful_calls": 0,
                            "failed_calls": 0,
                            "avg_execution_time": 0,
                            "execution_times": []
                        }
                    
                    tool_stats[tool_name]["total_calls"] += 1
                    
                    if run.status == "success":
                        tool_stats[tool_name]["successful_calls"] += 1
                    else:
                        tool_stats[tool_name]["failed_calls"] += 1
                    
                    if run.end_time and run.start_time:
                        duration = (run.end_time - run.start_time).total_seconds()
                        tool_stats[tool_name]["execution_times"].append(duration)
            
            # Calculate averages
            for tool_name, stats in tool_stats.items():
                if stats["execution_times"]:
                    stats["avg_execution_time"] = round(
                        sum(stats["execution_times"]) / len(stats["execution_times"]), 2
                    )
                stats["success_rate"] = round(
                    (stats["successful_calls"] / stats["total_calls"]) * 100, 2
                ) if stats["total_calls"] > 0 else 0
                # Remove execution_times list to reduce response size
                del stats["execution_times"]
            
            return {
                "tool_performance": tool_stats,
                "period_hours": hours,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting tool performance: {e}")
            return {"error": str(e)}
    
    def get_conversation_flow(self, hours: int = 24) -> Dict[str, Any]:
        """Get conversation flow analytics."""
        if not self.client:
            return {"error": "LangSmith not configured"}
        
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours)
            
            runs = list(self.client.list_runs(
                project_name=self.project,
                start_time=start_time,
                end_time=end_time,
                limit=1000
            ))
            
            # Group runs by thread_id
            conversations = {}
            for run in runs:
                if hasattr(run, 'extra') and run.extra and 'metadata' in run.extra:
                    metadata = run.extra['metadata']
                    thread_id = metadata.get('thread_id', 'unknown')
                    
                    if thread_id not in conversations:
                        conversations[thread_id] = {
                            "messages": 0,
                            "tools_used": set(),
                            "duration": 0,
                            "status": "active"
                        }
                    
                    conversations[thread_id]["messages"] += 1
                    
                    if run.name:
                        conversations[thread_id]["tools_used"].add(run.name)
                    
                    if run.end_time and run.start_time:
                        duration = (run.end_time - run.start_time).total_seconds()
                        conversations[thread_id]["duration"] += duration
                    
                    if run.status == "error":
                        conversations[thread_id]["status"] = "error"
            
            # Convert sets to lists for JSON serialization
            for conv in conversations.values():
                conv["tools_used"] = list(conv["tools_used"])
            
            return {
                "total_conversations": len(conversations),
                "conversation_details": conversations,
                "period_hours": hours,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting conversation flow: {e}")
            return {"error": str(e)}
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get LangSmith connection health status."""
        if not self.client:
            return {
                "status": "disconnected",
                "message": "LangSmith API key not configured"
            }
        
        try:
            # Test connection by listing projects
            projects = list(self.client.list_projects(limit=1))
            
            return {
                "status": "connected",
                "message": "LangSmith connection successful",
                "project": self.project,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"LangSmith health check failed: {e}")
            return {
                "status": "error",
                "message": str(e),
                "timestamp": datetime.now().isoformat()
            }


# Global monitor instance
langsmith_monitor = LangSmithMonitor()
