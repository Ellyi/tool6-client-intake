from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import uuid

load_dotenv()

app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": ["https://carspital.co.ke", "https://eliombogo.com", "http://localhost:*"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# Database connection
def get_db_connection():
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        cursor_factory=RealDictCursor
    )
    return conn

# Claude client
claude_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# System prompt for conversation
SYSTEM_PROMPT = """You are Nuru, the intelligent client intake assistant for LocalOS.

YOUR ROLE:
Qualify potential clients by understanding their business context, identifying real problems, and detecting cultural/payment/communication patterns.

CONTEXT DETECTION:
- Location signals: City names, currencies (Naira, Shilling, Rupee, Dollar)
- Payment context: M-Pesa (Kenya), UPI (India), Stripe (Western), Bank transfer (developing markets)
- Communication: WhatsApp-first (Africa/Asia), Email (Western/formal)
- Language: Detect code-switching, pidgin, sheng, formal English

CONVERSATION APPROACH:
1. Ask about their business and problem
2. Detect context signals naturally
3. Adapt recommendations based on location/payment/communication patterns
4. Qualify based on budget (>$3K = qualified) and timeline (>2 weeks = realistic)
5. Be conversational, not robotic

QUALIFICATION:
- Budget >$3K + realistic timeline = collect contact info, notify Eli
- Budget <$1K = provide resources, don't escalate
- Vague/browsing = politely probe, don't waste time

Be helpful to everyone, but protective of Eli's time."""


@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')
        session_id = data.get('session_id')
        
        if not session_id:
            session_id = str(uuid.uuid4())
        
        # Get conversation history
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Create conversation if new
        cur.execute(
            "INSERT INTO conversations (session_id) VALUES (%s) ON CONFLICT (session_id) DO NOTHING",
            (session_id,)
        )
        
        # Get conversation_id
        cur.execute("SELECT id FROM conversations WHERE session_id = %s", (session_id,))
        conversation = cur.fetchone()
        conversation_id = conversation['id']
        
        # Save user message
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, 'user', user_message)
        )
        
        # Get conversation history for context
        cur.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,)
        )
        history = cur.fetchall()
        
        # Build messages for Claude
        messages = []
        for msg in history:
            messages.append({
                "role": msg['role'],
                "content": msg['content']
            })
        
        # Call Claude
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=messages
        )
        
        assistant_message = response.content[0].text
        
        # Save assistant message
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, 'assistant', assistant_message)
        )
        
        # Detect context (simplified for now)
        detect_and_save_context(conversation_id, user_message, assistant_message, cur)
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            'response': assistant_message,
            'session_id': session_id
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def detect_and_save_context(conversation_id, user_msg, assistant_msg, cursor):
    """Detect context signals from conversation"""
    
    combined_text = (user_msg + " " + assistant_msg).lower()
    
    # Location detection
    location = None
    if 'nairobi' in combined_text or 'kenya' in combined_text:
        location = 'Kenya'
    elif 'lagos' in combined_text or 'nigeria' in combined_text:
        location = 'Nigeria'
    elif 'mumbai' in combined_text or 'india' in combined_text:
        location = 'India'
    
    # Payment detection
    payment = None
    if 'm-pesa' in combined_text or 'mpesa' in combined_text:
        payment = 'M-Pesa'
    elif 'upi' in combined_text:
        payment = 'UPI'
    elif 'stripe' in combined_text:
        payment = 'Stripe'
    
    # Communication detection
    communication = None
    if 'whatsapp' in combined_text:
        communication = 'WhatsApp'
    elif 'email' in combined_text:
        communication = 'Email'
    
    # Save if we detected anything
    if location or payment or communication:
        cursor.execute(
            """INSERT INTO context_data 
               (conversation_id, location, payment_method, communication_channel)
               VALUES (%s, %s, %s, %s)""",
            (conversation_id, location, payment, communication)
        )


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    app.run(debug=True, port=5000)