import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

# Read schema file
with open('../schema.sql', 'r') as f:
    schema = f.read()

# Connect to database
conn = psycopg2.connect(
    host=os.getenv('DB_HOST'),
    database=os.getenv('DB_NAME'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    port=os.getenv('DB_PORT', 5432)
)

cur = conn.cursor()

# Execute schema
cur.execute(schema)
conn.commit()

print("✅ Database tables created successfully!")

cur.close()
conn.close()