import sys
import os
from dotenv import load_dotenv

# Add the backend directory so app modules can be imported.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.reflection import ReflectionService

def main():
    # Load the unified environment file for local execution.
    root_env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
    if os.path.exists(root_env_path):
        load_dotenv(root_env_path, override=False)

    """
    运行自动反思验证任务
    建议通过 cron 或 定时任务调度器 每天运行一次
    """
    print("Running Automated Reflection Verification Task...")
    service = ReflectionService()
    stats = service.run_verification_cycle()
    print("Reflection stats:", stats)
    print("Task Completed.")

if __name__ == "__main__":
    main()

