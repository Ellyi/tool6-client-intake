from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import uuid
import requests

load_dotenv()

app = Flask(__name__)
CORS(app)

# Database connection
def get_db_connection():
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        port=os.getenv('DB_PORT'),
        cursor_factory=RealDictCursor
    )
    return conn

# Claude client
claude_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# Load context from Tools #3, #4, #5
def load_audit_context(session_id):
    """Load audit context from other tools"""
    contexts = {}
    
    # Try Tool #3 (Business Intelligence Auditor)
    try:
        response = requests.get(
            f'https://tool3-business-intel-backend-production.up.railway.app/api/session/{session_id}',
            timeout=3
        )
        if response.status_code == 200:
            contexts['tool3'] = response.json()
    except:
        pass
    
    # Try Tool #4 (AI Readiness Scanner)
    try:
        response = requests.get(
            f'https://tool4-ai-readiness-production.up.railway.app/api/session/{session_id}',
            timeout=3
        )
        if response.status_code == 200:
            contexts['tool4'] = response.json()
    except:
        pass
    
    # Try Tool #5 (ROI Projector)
    try:
        response = requests.get(
            f'https://tool5-roi-projector-production.up.railway.app/api/session/{session_id}',
            timeout=3
        )
        if response.status_code == 200:
            contexts['tool5'] = response.json()
    except:
        pass
    
    return contexts

# Auto-create tables on first run
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(255) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                role VARCHAR(50) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                email VARCHAR(255),
                phone VARCHAR(50),
                budget VARCHAR(100),
                timeline VARCHAR(100),
                qualified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS context_data (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                location VARCHAR(100),
                payment_method VARCHAR(100),
                communication_channel VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database tables ready")
    except Exception as e:
        print(f"DB init: {e}")

init_db()

# System prompt with Tentacles approach
SYSTEM_PROMPT = """You are Nuru, the intelligent client intake assistant for LocalOS.

CONTEXT AWARENESS - CRITICAL:
If you receive audit context in the conversation:
- Tool #3 data = Intelligence waste audit (waste_score, top waste zones, hours wasted)
- Tool #4 data = AI readiness assessment (readiness_score, blocking factors)
- Tool #5 data = ROI projection (annual_savings, payback_months)

ALWAYS reference their specific results naturally:
"I see from your audit that [specific finding]. Let me help you with that..."

YOUR ROLE:
Qualify potential clients by understanding their business context, identifying real problems, and detecting cultural/payment/communication patterns.

TENTACLES BUDGET APPROACH (CRITICAL):
- NO budget gatekeeping - welcome ANY serious budget
- Scope adapts to budget: $500 = micro-solution, $5K = full workflow, $20K = platform
- When user states budget, respond with:
  "With [budget], here's what we can build: [scope]
   For comparison, [higher budget] gets you: [more scope]
   Which matches your needs right now?"
- Natural education through options, not rejection
- Show Tentacles capability: "We can build this using [components]"

QUALIFICATION CRITERIA:
✅ ESCALATE TO ELI when:
- Budget stated (any amount) + Timeline realistic + Problem clear
- Serious signals: specific pain, asked intelligent questions, completed tools

❌ DON'T ESCALATE when:
- Vague answers only ("just exploring", "maybe someday")
- Unrealistic timeline + no flexibility
- No clear problem articulated
- Tire-kicker signals

CONTEXT DETECTION (GLOBAL):
- Location: USA, UK, Canada, Australia, South Africa, UAE, China, Germany, France, Brazil, Mexico, Singapore, Philippines, Egypt, Kenya, Nigeria, India
- Payment: Stripe, PayPal, M-Pesa, UPI, WeChat Pay, Alipay, PIX, GCash, Zelle, Bank Transfer, SEPA
- Communication: WhatsApp (Africa/Asia/LatAm), Email (Western/formal), WeChat (China)
- Adapt recommendations based on real-world context

CONVERSATION APPROACH:
1. Reference audit results if available (builds instant credibility)
2. Ask about their business and specific problem
3. Detect context signals naturally (location, payment, communication)
4. When budget discussed, show Tentacles options at that budget
5. Qualify based on seriousness, not budget size
6. Be conversational, helpful, real - not robotic

Be helpful to EVERYONE, but protective of Eli's time against tire-kickers."""


@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')
        session_id = data.get('session_id')
        
        if not session_id:
            session_id = str(uuid.uuid4())
        
        # Load audit context from other tools
        audit_contexts = load_audit_context(session_id)
        
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
        
        # If first message AND we have audit context, inject it
        if len(history) == 1 and audit_contexts:
            context_message = "[AUDIT CONTEXT AVAILABLE]\n"
            
            if 'tool3' in audit_contexts:
                ctx = audit_contexts['tool3']
                context_message += f"\nTool #3 Intelligence Audit:\n"
                context_message += f"- Company: {ctx.get('company_name')}\n"
                context_message += f"- Industry: {ctx.get('industry')}\n"
                context_message += f"- Waste Score: {ctx.get('waste_score')}/100\n"
                context_message += f"- Hours Wasted Monthly: {ctx.get('total_hours_wasted')}\n"
                if ctx.get('top_waste_zones'):
                    zones = [z.get('name') for z in ctx['top_waste_zones'][:3]]
                    context_message += f"- Top Waste Zones: {', '.join(zones)}\n"
            
            if 'tool4' in audit_contexts:
                ctx = audit_contexts['tool4']
                context_message += f"\nTool #4 AI Readiness:\n"
                context_message += f"- Readiness Score: {ctx.get('readiness_score')}/100\n"
                if ctx.get('blocking_factors'):
                    context_message += f"- Blocking Factors: {', '.join(ctx['blocking_factors'])}\n"
            
            if 'tool5' in audit_contexts:
                ctx = audit_contexts['tool5']
                context_message += f"\nTool #5 ROI Projection:\n"
                context_message += f"- Annual Savings: ${ctx.get('annual_savings'):,}\n"
                context_message += f"- Implementation Cost: ${ctx.get('implementation_cost'):,}\n"
                context_message += f"- Payback Period: {ctx.get('payback_months')} months\n"
            
            # Inject context before user's first message
            messages.append({
                "role": "user",
                "content": context_message + f"\n\nUser's first message: {user_message}"
            })
        else:
            # Regular conversation flow
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
        
        # Detect context (expanded globally)
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
    """Detect context signals from conversation - GLOBAL coverage"""
    
    combined_text = (user_msg + " " + assistant_msg).lower()
    
    # Location detection - EXPANDED GLOBALLY
    location = None
    
    # Africa
    if 'nairobi' in combined_text or 'kenya' in combined_text:
        location = 'Kenya'
    elif 'lagos' in combined_text or 'nigeria' in combined_text:
        location = 'Nigeria'
    elif 'johannesburg' in combined_text or 'cape town' in combined_text or 'south africa' in combined_text:
        location = 'South Africa'
    elif 'cairo' in combined_text or 'egypt' in combined_text:
        location = 'Egypt'
    
    # Asia
    elif 'mumbai' in combined_text or 'india' in combined_text or 'delhi' in combined_text:
        location = 'India'
    elif 'beijing' in combined_text or 'shanghai' in combined_text or 'china' in combined_text:
        location = 'China'
    elif 'singapore' in combined_text:
        location = 'Singapore'
    elif 'manila' in combined_text or 'philippines' in combined_text:
        location = 'Philippines'
    
    # Middle East
    elif 'dubai' in combined_text or 'abu dhabi' in combined_text or 'uae' in combined_text:
        location = 'UAE'
    
    # Americas
    elif 'new york' in combined_text or 'los angeles' in combined_text or 'chicago' in combined_text or 'san francisco' in combined_text or 'usa' in combined_text or 'united states' in combined_text:
        location = 'USA'
    elif 'toronto' in combined_text or 'vancouver' in combined_text or 'canada' in combined_text:
        location = 'Canada'
    elif 'são paulo' in combined_text or 'rio' in combined_text or 'brazil' in combined_text:
        location = 'Brazil'
    elif 'mexico city' in combined_text or 'mexico' in combined_text:
        location = 'Mexico'
    
    # Europe
    elif 'london' in combined_text or 'manchester' in combined_text or 'uk' in combined_text or 'united kingdom' in combined_text:
        location = 'UK'
    elif 'berlin' in combined_text or 'munich' in combined_text or 'germany' in combined_text:
        location = 'Germany'
    elif 'paris' in combined_text or 'france' in combined_text:
        location = 'France'
    
    # Oceania
    elif 'sydney' in combined_text or 'melbourne' in combined_text or 'australia' in combined_text:
        location = 'Australia'
    
    # Payment detection - EXPANDED GLOBALLY
    payment = None
    if 'm-pesa' in combined_text or 'mpesa' in combined_text:
        payment = 'M-Pesa'
    elif 'upi' in combined_text:
        payment = 'UPI'
    elif 'stripe' in combined_text:
        payment = 'Stripe'
    elif 'paypal' in combined_text:
        payment = 'PayPal'
    elif 'wechat pay' in combined_text or 'wechat' in combined_text:
        payment = 'WeChat Pay'
    elif 'alipay' in combined_text:
        payment = 'Alipay'
    elif 'pix' in combined_text:
        payment = 'PIX'
    elif 'gcash' in combined_text:
        payment = 'GCash'
    elif 'zelle' in combined_text:
        payment = 'Zelle'
    elif 'sepa' in combined_text:
        payment = 'SEPA'
    elif 'bank transfer' in combined_text:
        payment = 'Bank Transfer'
    
    # Communication detection
    communication = None
    if 'whatsapp' in combined_text:
        communication = 'WhatsApp'
    elif 'email' in combined_text:
        communication = 'Email'
    elif 'wechat' in combined_text:
        communication = 'WeChat'
    
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