import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env from backend folder
backend_dir = Path(__file__).parent / 'backend'
env_file = backend_dir / '.env'

if env_file.exists():
    load_dotenv(env_file)
    print(f"✅ Loaded .env from {env_file}")
else:
    # Try current directory
    load_dotenv()
    print("✅ Loaded .env from current directory")

def update_schema():
    """Add notified_at column to leads table"""
    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST'),
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            port=os.getenv('DB_PORT')
        )
        
        cur = conn.cursor()
        
        # Add notified_at column if it doesn't exist
        cur.execute("""
            ALTER TABLE leads 
            ADD COLUMN IF NOT EXISTS notified_at TIMESTAMP;
        """)
        
        conn.commit()
        print("✅ Schema updated: notified_at column added to leads table")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ Schema update failed: {e}")

if __name__ == '__main__':
    update_schema()