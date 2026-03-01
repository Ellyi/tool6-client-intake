from flask import Flask, request, jsonify, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import anthropic
import os
import html
import bleach
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from datetime import datetime, timedelta
import uuid
import requests
import re
import threading
import hashlib
import hmac
from urllib.parse import quote

load_dotenv()
from utils.model_router import get_model

app = Flask(__name__)

# ============================================
# REQUEST SIZE LIMIT — 16KB max
# Prevents oversized payloads crashing the server
# ============================================
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024

CORS(app, origins=["https://eliombogo.com", "https://www.eliombogo.com"])

# ============================================
# RATE LIMITING
# Prevents API credit drain and abuse
# ============================================
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ============================================
# ADMIN SECRET KEY
# Protects /api/stats and /api/conversations
# Set ADMIN_SECRET in Railway vars
# ============================================
ADMIN_SECRET = os.getenv('ADMIN_SECRET', '')

def require_admin_key():
    """Check admin key from header or query param"""
    provided = request.headers.get('X-Admin-Key') or request.args.get('key')
    if not ADMIN_SECRET:
        return False, "ADMIN_SECRET not configured in Railway vars"
    if not provided:
        return False, "Missing admin key"
    if not hmac.compare_digest(provided, ADMIN_SECRET):
        return False, "Invalid admin key"
    return True, None

# ============================================
# CONNECTION POOLING
# One pool shared across requests — not one connection per request
# ============================================
db_pool = None

def init_pool():
    global db_pool
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=os.getenv('DB_HOST'),
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            port=os.getenv('DB_PORT')
        )
        print("✅ Connection pool initialised (2-10 connections)")
    except Exception as e:
        print(f"❌ Pool init failed: {e}")

def get_db_connection():
    if db_pool:
        conn = db_pool.getconn()
        conn.cursor_factory = RealDictCursor
        return conn
    # Fallback to direct connection if pool failed
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        port=os.getenv('DB_PORT'),
        cursor_factory=RealDictCursor
    )

def release_db_connection(conn):
    if db_pool:
        db_pool.putconn(conn)
    else:
        conn.close()

# ============================================
# INPUT SANITIZATION
# Strip HTML, limit length, remove control chars
# ============================================
ALLOWED_TAGS = []  # No HTML allowed in chat messages

def sanitize_input(text, max_length=2000):
    """Sanitize user input — strip HTML, limit length, clean control chars"""
    if not text:
        return ''
    # Strip HTML tags
    text = bleach.clean(text, tags=ALLOWED_TAGS, strip=True)
    # Remove null bytes and control characters (except newlines/tabs)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Limit length
    text = text[:max_length]
    # Strip leading/trailing whitespace
    return text.strip()

# ============================================
# CLAUDE CLIENT
# ============================================
claude_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# ============================================
# SYSTEM PROMPT — loaded from file
# ============================================
def load_system_prompt():
    try:
        with open('system_prompt.txt', 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        try:
            with open('../system_prompt.txt', 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            print("WARNING: system_prompt.txt not found, using fallback")
            return """You are Nuru, the intelligent client intake assistant for LocalOS.
Qualify potential clients by understanding their business context and identifying real problems.
Be helpful, conversational, and honest. Escalate complex/high-value opportunities to Eli."""

SYSTEM_PROMPT = load_system_prompt()
print(f"✅ System prompt loaded ({len(SYSTEM_PROMPT)} characters)")

# ============================================
# AUDIT CONTEXT LOADER — with caching
# Prevents repeated fetches on every message
# ============================================
_context_cache = {}

def load_audit_context(session_id):
    """Load Tool 3/4/5 context with simple in-memory cache"""
    if session_id in _context_cache:
        cached_at, contexts = _context_cache[session_id]
        if datetime.now() - cached_at < timedelta(minutes=30):
            return contexts

    contexts = {}
    endpoints = {
        'tool3': f'https://tool3-business-intel-backend-production.up.railway.app/api/session/{session_id}',
        'tool4': f'https://tool4-ai-readiness-production.up.railway.app/api/session/{session_id}',
        'tool5': f'https://tool5-roi-projector-production.up.railway.app/api/session/{session_id}',
    }
    for key, url in endpoints.items():
        try:
            response = requests.get(url, timeout=3)
            if response.status_code == 200:
                contexts[key] = response.json()
        except:
            pass

    _context_cache[session_id] = (datetime.now(), contexts)
    return contexts

# ============================================
# WHATSAPP — CallMeBot
# ============================================
def send_whatsapp_notification(message):
    api_key = os.getenv('CALLMEBOT_API_KEY')
    phone = os.getenv('WHATSAPP_PHONE', '254701475000')
    if not api_key:
        print("WARNING: CALLMEBOT_API_KEY not set — WhatsApp skipped")
        return False
    try:
        encoded_message = quote(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded_message}&apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            print(f"✅ WhatsApp sent to +{phone}")
            return True
        else:
            print(f"❌ CallMeBot error: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ WhatsApp failed: {e}")
        return False

# ============================================
# EMAIL — Resend API with retry
# ============================================
def send_email_notification(subject, body_text, body_html=None, retries=2):
    resend_api_key = os.getenv('RESEND_API_KEY')
    notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
    from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')
    if not resend_api_key:
        print("WARNING: RESEND_API_KEY not set")
        return False
    payload = {
        'from': from_email,
        'to': [notify_email],
        'subject': subject,
        'html': body_html if body_html else body_text.replace('\n', '<br>'),
        'text': body_text
    }
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                'https://api.resend.com/emails',
                headers={'Authorization': f'Bearer {resend_api_key}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=10
            )
            if response.status_code in [200, 201]:
                print(f"✅ Email sent via Resend to {notify_email}")
                return True
            else:
                print(f"❌ Resend error (attempt {attempt+1}): {response.status_code}")
        except Exception as e:
            print(f"❌ Email attempt {attempt+1} failed: {e}")
    return False

# ============================================
# BACKGROUND NOTIFICATION
# Runs email + WhatsApp in background thread
# Chat response is not blocked by slow email API
# ============================================
def notify_in_background(fn, *args, **kwargs):
    thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    thread.start()

# ============================================
# TOOL COMPLETION EMAIL
# ============================================
def send_tool_completion_email(user_email, tool_number, result_data):
    resend_api_key = os.getenv('RESEND_API_KEY')
    from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')
    if not resend_api_key or not user_email:
        return False

    if tool_number == 3:
        subject = f"Your Intelligence Waste Audit Results — Score: {result_data.get('waste_score', 0)}/100"
        headline = "Your Business Intelligence Audit is Complete"
        summary_lines = [
            f"Waste Score: {result_data.get('waste_score', 0)}/100",
            f"Hours Wasted Monthly: {result_data.get('total_hours_wasted', 'N/A')}",
            f"Estimated Annual Cost: ${result_data.get('annual_cost', 0):,}",
        ]
        if result_data.get('top_waste_zones'):
            zones = [z.get('name', '') for z in result_data['top_waste_zones'][:3]]
            summary_lines.append(f"Top Waste Zones: {', '.join(zones)}")
        cta_text = "Talk to Nuru — Get Your Fix Plan"
    elif tool_number == 4:
        subject = f"Your AI Readiness Score — {result_data.get('readiness_score', 0)}/100"
        headline = "Your AI Readiness Scan is Complete"
        summary_lines = [f"Readiness Score: {result_data.get('readiness_score', 0)}/100"]
        if result_data.get('blocking_factors'):
            summary_lines.append(f"Key Blockers: {', '.join(result_data['blocking_factors'][:3])}")
        cta_text = "Talk to Nuru — See How to Improve"
    elif tool_number == 5:
        subject = f"Your ROI Projection — ${result_data.get('annual_savings', 0):,} potential annual savings"
        headline = "Your ROI Projection is Ready"
        summary_lines = [
            f"Projected Annual Savings: ${result_data.get('annual_savings', 0):,}",
            f"Implementation Cost: ${result_data.get('implementation_cost', 0):,}",
            f"Payback Period: {result_data.get('payback_months', 'N/A')} months",
        ]
        cta_text = "Talk to Nuru — Start Your Project"
    else:
        return False

    nuru_url = "https://eliombogo.com/#nuru"
    if result_data.get('session_id'):
        nuru_url = f"https://eliombogo.com/#nuru?session={result_data['session_id']}"

    summary_html = ''.join([f"<p style='margin:6px 0;'>✅ <strong>{html.escape(line)}</strong></p>" for line in summary_lines])
    summary_text = '\n'.join([f"✅ {line}" for line in summary_lines])

    body_html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#1a2332;color:white;padding:24px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;color:#10b981;">LocalOS Intelligence Platform</h2>
    <p style="margin:8px 0 0;color:#9ca3af;font-size:14px;">{html.escape(headline)}</p>
  </div>
  <div style="background:#f9fafb;padding:24px;border:1px solid #e5e7eb;">
    <h3 style="color:#1a2332;margin-top:0;">Your Results</h3>
    {summary_html}
  </div>
  <div style="background:white;padding:24px;border:1px solid #e5e7eb;border-top:none;">
    <p style="color:#374151;margin-top:0;">Nuru has analysed your results and can show you exactly what to fix first.</p>
    <div style="text-align:center;margin:24px 0;">
      <a href="{nuru_url}" style="background:#10b981;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:bold;font-size:16px;display:inline-block;">{html.escape(cta_text)}</a>
    </div>
    <p style="color:#6b7280;font-size:13px;text-align:center;">Or book a discovery call: <a href="https://calendly.com/eli-eliombogo/discovery-call" style="color:#10b981;">calendly.com/eli-eliombogo/discovery-call</a></p>
  </div>
  <div style="background:#1a2332;padding:16px;border-radius:0 0 8px 8px;text-align:center;">
    <p style="color:#9ca3af;font-size:12px;margin:0;">LocalOS — Intelligence Waste Auditors | eliombogo.com</p>
  </div>
</div>"""

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend_api_key}', 'Content-Type': 'application/json'},
            json={'from': from_email, 'to': [user_email], 'subject': subject, 'html': body_html, 'text': summary_text},
            timeout=10
        )
        if response.status_code in [200, 201]:
            print(f"✅ Tool #{tool_number} completion email sent to {user_email}")
            return True
        return False
    except Exception as e:
        print(f"❌ Tool completion email error: {e}")
        return False

# ============================================
# NOTIFY COMPLETION ENDPOINT
# ============================================
@app.route('/api/notify-completion', methods=['POST'])
@limiter.limit("30 per hour")
def notify_completion():
    try:
        data = request.json
        tool_number = data.get('tool_number')
        user_email = sanitize_input(data.get('user_email', ''), max_length=255)
        session_id = sanitize_input(data.get('session_id', ''), max_length=255)
        result_data = data.get('result_data', {})

        if not tool_number or not user_email:
            return jsonify({'error': 'tool_number and user_email are required'}), 400

        # Validate email format
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', user_email):
            return jsonify({'error': 'Invalid email format'}), 400

        result_data['session_id'] = session_id

        notify_in_background(send_tool_completion_email, user_email, tool_number, result_data)
        notify_in_background(send_whatsapp_notification,
            f"🔔 Tool #{tool_number} completed\nEmail: {user_email}\nSession: {session_id}"
        )

        return jsonify({'success': True, 'message': f'Tool #{tool_number} notification queued for {user_email}'})
    except Exception as e:
        print(f"❌ notify_completion error: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================
# LEAD DATA EXTRACTION — budget-aware
# Distinguishes loss figures from actual budget signals
# ============================================
def extract_lead_data_from_history(conversation_id, cursor):
    cursor.execute(
        "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
        (conversation_id,)
    )
    messages = cursor.fetchall()

    user_text = ' '.join([m['content'] for m in messages if m['role'] == 'user'])
    all_text = ' '.join([m['content'] for m in messages])

    lead_data = {}

    # Email
    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_text)
    if email_match:
        lead_data['email'] = email_match.group(0)

    # Phone
    phone_match = re.search(r'(\+?\d[\d\s\-().]{7,}\d)', user_text)
    if phone_match:
        lead_data['phone'] = phone_match.group(0).strip()

    # Budget — ONLY match phrases that indicate willingness to spend
    # NOT loss figures, cost mentions, or problem descriptions
    budget_pattern = re.search(
        r'(?:budget|willing to spend|have|allocated|set aside|approved)[^\d$]{0,20}\$?([\d,]+[kK]?)'
        r'|\b([\d,]+[kK]?)\s*(?:budget|to spend|available|to invest)',
        user_text, re.IGNORECASE
    )
    if budget_pattern:
        matched = budget_pattern.group(1) or budget_pattern.group(2)
        lead_data['budget'] = f"${matched.strip()}"

    # Company
    company_patterns = [
        r"(?:my company|our company|company name|we(?:'re| are) (?:at |called |named )?|i work (?:at|for) |business (?:is |called )?)([\w\s&.,'-]{2,40}?)(?:\.|,|\s+and|\s+we|\s+our|\s+i |$)",
        r"(?:at|for|from)\s+([A-Z][a-zA-Z\s&.,'-]{2,30}?)(?:\s+(?:and|we|our|i )|[.,]|$)"
    ]
    for pattern in company_patterns:
        match = re.search(pattern, user_text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().rstrip('.,')
            if len(candidate) > 2 and candidate.lower() not in ['the', 'and', 'but', 'that', 'this']:
                lead_data['company'] = candidate
                break

    # Industry
    industry_keywords = {
        'logistics': 'Logistics', 'transport': 'Transport', 'shipping': 'Logistics',
        'port': 'Logistics', 'freight': 'Logistics', 'clearing': 'Logistics',
        'legal': 'Legal', 'law firm': 'Legal', 'advocate': 'Legal', 'court': 'Legal',
        'healthcare': 'Healthcare', 'hospital': 'Healthcare', 'clinic': 'Healthcare',
        'finance': 'Finance', 'fintech': 'Fintech', 'banking': 'Finance',
        'retail': 'Retail', 'ecommerce': 'E-commerce', 'e-commerce': 'E-commerce',
        'saas': 'SaaS', 'software': 'Software', 'tech': 'Technology',
        'manufacturing': 'Manufacturing', 'factory': 'Manufacturing',
        'real estate': 'Real Estate', 'property': 'Real Estate',
        'education': 'Education', 'school': 'Education',
        'restaurant': 'Food & Beverage', 'food': 'Food & Beverage',
        'consulting': 'Consulting', 'agency': 'Agency',
        'agriculture': 'Agriculture', 'farming': 'Agriculture',
    }
    lower_text = all_text.lower()
    for keyword, industry in industry_keywords.items():
        if keyword in lower_text:
            lead_data['industry'] = industry
            break

    # Problem — first substantive user message
    user_messages = [m['content'] for m in messages if m['role'] == 'user']
    for msg in user_messages:
        if len(msg) > 20:
            lead_data['problem'] = msg[:300] + ('...' if len(msg) > 300 else '')
            break

    # Timeline
    timeline_patterns = ['this week', 'this month', 'next month', 'asap', 'urgent',
                         'q1', 'q2', 'q3', 'q4', '2 weeks', '1 month', '3 months', '6 months']
    for pattern in timeline_patterns:
        if pattern in lower_text:
            lead_data['timeline'] = pattern
            break

    return lead_data

# ============================================
# NOTIFY ELI — QUALIFIED LEAD
# Runs in background thread
# ============================================
def notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts):
    try:
        body = f"""QUALIFIED LEAD - LocalOS
{'='*50}

LEAD DETAILS:
Company: {lead_data.get('company', 'Not captured yet')}
Industry: {lead_data.get('industry', 'Not captured yet')}
Contact: {lead_data.get('email', 'Not captured yet')}
Phone: {lead_data.get('phone', 'Not captured yet')}

QUALIFICATION:
Budget: {lead_data.get('budget', 'Mentioned in conversation')}
Timeline: {lead_data.get('timeline', 'Not stated')}
Problem: {lead_data.get('problem', 'See conversation')}

AUDIT DATA:"""

        if 'tool3' in audit_contexts:
            ctx = audit_contexts['tool3']
            body += f"\nTool #3 Waste Score: {ctx.get('waste_score')}/100"
            body += f"\nTop Waste Zone: {ctx['top_waste_zones'][0]['name'] if ctx.get('top_waste_zones') else 'N/A'}"
            body += f"\nHours Wasted: {ctx.get('total_hours_wasted')}/month"
        if 'tool4' in audit_contexts:
            body += f"\nTool #4 Readiness: {audit_contexts['tool4'].get('readiness_score')}/100"
        if 'tool5' in audit_contexts:
            savings = audit_contexts['tool5'].get('annual_savings', 0)
            body += f"\nTool #5 ROI: ${savings:,} annual savings"

        body += f"""

CONVERSATION ID: {conversation_id}
TIME: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

ACTION: Book discovery call — calendly.com/eli-eliombogo/discovery-call
"""

        html_body = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#1a2332;color:white;padding:20px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;color:#10b981;">🎯 Qualified Lead - LocalOS</h2>
    <p style="margin:5px 0 0;color:#9ca3af;font-size:14px;">{datetime.now().strftime('%B %d, %Y at %H:%M UTC')}</p>
  </div>
  <div style="background:#f9fafb;padding:20px;border:1px solid #e5e7eb;">
    <h3 style="color:#1a2332;border-bottom:2px solid #10b981;padding-bottom:8px;">Lead Details</h3>
    <p><strong>Company:</strong> {html.escape(str(lead_data.get('company', 'Not captured yet')))}</p>
    <p><strong>Industry:</strong> {html.escape(str(lead_data.get('industry', 'Not captured yet')))}</p>
    <p><strong>Contact:</strong> {html.escape(str(lead_data.get('email', 'Not captured yet')))}</p>
    <p><strong>Phone:</strong> {html.escape(str(lead_data.get('phone', 'Not captured yet')))}</p>
    <p><strong>Budget:</strong> {html.escape(str(lead_data.get('budget', 'Mentioned in conversation')))}</p>
    <p><strong>Timeline:</strong> {html.escape(str(lead_data.get('timeline', 'Not stated')))}</p>
    <p><strong>Problem:</strong> {html.escape(str(lead_data.get('problem', 'See conversation')))}</p>
  </div>
  <div style="background:#1a2332;padding:20px;border-radius:0 0 8px 8px;text-align:center;">
    <a href="https://calendly.com/eli-eliombogo/discovery-call"
       style="background:#10b981;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;display:inline-block;">
      Book Discovery Call
    </a>
    <p style="color:#9ca3af;font-size:12px;margin-top:12px;">Conversation ID: {conversation_id} | Nuru - LocalOS</p>
  </div>
</div>"""

        send_email_notification(
            subject=f"🎯 Qualified Lead - LocalOS | Conversation {conversation_id}",
            body_text=body,
            body_html=html_body
        )

        send_whatsapp_notification(
            f"🎯 QUALIFIED LEAD\n"
            f"Company: {lead_data.get('company', 'Unknown')}\n"
            f"Budget: {lead_data.get('budget', 'Not stated')}\n"
            f"Contact: {lead_data.get('email', 'Not captured')}\n"
            f"Problem: {str(lead_data.get('problem', ''))[:100]}\n"
            f"Check email for full brief."
        )

        try:
            requests.post(
                "https://script.google.com/macros/s/AKfycbw_DUBZMbh47xMP5Lg83Q04o66oDQFwdO6qM7pixoN4BzVLkR9iz4EiT2WrPU2NTAANlw/exec",
                json={
                    'type': 'qualified_lead',
                    'timestamp': datetime.now().isoformat(),
                    'message': body,
                    'lead_data': lead_data,
                    'conversation_id': str(conversation_id)
                },
                timeout=5
            )
        except Exception as webhook_error:
            print(f"Webhook failed: {webhook_error}")

    except Exception as e:
        print(f"Failed to notify Eli: {e}")

# ============================================
# DATABASE INIT
# ============================================
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(255) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'active',
                lead_quality_score INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                business_name VARCHAR(255),
                contact_name VARCHAR(255),
                email VARCHAR(255),
                phone VARCHAR(50),
                problem_description TEXT,
                budget_range VARCHAR(100),
                timeline VARCHAR(100),
                location VARCHAR(255),
                payment_context VARCHAR(100),
                communication_preference VARCHAR(100),
                language_detected VARCHAR(50),
                qualification_status VARCHAR(50) DEFAULT 'pending',
                notified_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS context_data (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                location VARCHAR(255),
                payment_method VARCHAR(100),
                communication_channel VARCHAR(100),
                language VARCHAR(50),
                industry VARCHAR(255),
                tech_stack TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON conversations(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_leads_conversation_id ON leads(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_leads_notified ON leads(notified_at);
        """)
        conn.commit()
        cur.close()
        release_db_connection(conn)
        print("✅ Database tables and indexes ready")
    except Exception as e:
        print(f"DB init error: {e}")

# ============================================
# CHAT ENDPOINT
# Rate limited: 30 messages/hour per IP
# ============================================
@app.route('/api/chat', methods=['POST'])
@limiter.limit("30 per hour")
def chat():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'Invalid JSON'}), 400

        raw_message = data.get('message', '')
        user_message = sanitize_input(raw_message, max_length=2000)
        session_id = sanitize_input(data.get('session_id', ''), max_length=255)

        if not user_message:
            return jsonify({'error': 'Message cannot be empty'}), 400

        if not session_id:
            session_id = str(uuid.uuid4())

        audit_contexts = load_audit_context(session_id)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            "INSERT INTO conversations (session_id) VALUES (%s) ON CONFLICT (session_id) DO NOTHING",
            (session_id,)
        )

        cur.execute("SELECT id FROM conversations WHERE session_id = %s", (session_id,))
        conversation = cur.fetchone()
        conversation_id = conversation['id']

        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, 'user', user_message)
        )

        # Check conversation age — expire sessions older than 30 days
        cur.execute("SELECT created_at FROM conversations WHERE id = %s", (conversation_id,))
        conv_record = cur.fetchone()
        if conv_record and (datetime.now() - conv_record['created_at'].replace(tzinfo=None)) > timedelta(days=30):
            conn.commit()
            cur.close()
            release_db_connection(conn)
            return jsonify({'response': "It's been a while — let's start fresh. What are you working on?", 'session_id': str(uuid.uuid4())})

        cur.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,)
        )
        history = cur.fetchall()

        messages = []
        if len(history) == 1 and audit_contexts:
            context_message = "[AUDIT CONTEXT AVAILABLE]\n"
            if 'tool3' in audit_contexts:
                ctx = audit_contexts['tool3']
                context_message += f"\nTool #3 Intelligence Audit:"
                context_message += f"\n- Company: {ctx.get('company_name')}"
                context_message += f"\n- Industry: {ctx.get('industry')}"
                context_message += f"\n- Waste Score: {ctx.get('waste_score')}/100"
                context_message += f"\n- Hours Wasted Monthly: {ctx.get('total_hours_wasted')}"
                if ctx.get('top_waste_zones'):
                    zones = [z.get('name') for z in ctx['top_waste_zones'][:3]]
                    context_message += f"\n- Top Waste Zones: {', '.join(zones)}"
            if 'tool4' in audit_contexts:
                ctx = audit_contexts['tool4']
                context_message += f"\nTool #4 AI Readiness: {ctx.get('readiness_score')}/100"
                if ctx.get('blocking_factors'):
                    context_message += f"\n- Blocking Factors: {', '.join(ctx['blocking_factors'])}"
            if 'tool5' in audit_contexts:
                ctx = audit_contexts['tool5']
                context_message += f"\nTool #5 ROI: ${ctx.get('annual_savings'):,} annual savings | Payback: {ctx.get('payback_months')} months"
            messages.append({"role": "user", "content": context_message + f"\n\nUser's first message: {user_message}"})
        else:
            for msg in history:
                messages.append({"role": msg['role'], "content": msg['content']})

        response = claude_client.messages.create(
            model=get_model("nuru_chat"),
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=messages
        )

        assistant_message = response.content[0].text

        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, 'assistant', assistant_message)
        )

        detect_and_save_context(conversation_id, user_message, assistant_message, cur)
        check_qualification(conversation_id, assistant_message, user_message, audit_contexts, cur)

        conn.commit()
        cur.close()
        release_db_connection(conn)

        return jsonify({'response': assistant_message, 'session_id': session_id})

    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({'error': 'Something went wrong. Please try again.'}), 500

# ============================================
# CONTEXT DETECTION
# ============================================
def detect_and_save_context(conversation_id, user_msg, assistant_msg, cursor):
    combined_text = (user_msg + " " + assistant_msg).lower()

    location_map = {
        'Kenya': ['nairobi', 'kenya', 'mombasa', 'kisumu'],
        'Nigeria': ['lagos', 'nigeria', 'abuja', 'port harcourt'],
        'South Africa': ['johannesburg', 'cape town', 'south africa', 'durban'],
        'Egypt': ['cairo', 'egypt'],
        'India': ['mumbai', 'india', 'delhi', 'bangalore', 'chennai'],
        'China': ['beijing', 'shanghai', 'china'],
        'Singapore': ['singapore'],
        'Philippines': ['manila', 'philippines'],
        'UAE': ['dubai', 'abu dhabi', 'uae'],
        'USA': ['new york', 'los angeles', 'chicago', 'san francisco', 'usa', 'united states', 'colorado'],
        'Canada': ['toronto', 'vancouver', 'canada'],
        'UK': ['london', 'manchester', 'uk', 'united kingdom'],
        'Germany': ['berlin', 'munich', 'germany'],
        'France': ['paris', 'france'],
        'Australia': ['sydney', 'melbourne', 'australia'],
        'Brazil': ['sao paulo', 'brazil', 'rio'],
        'Ghana': ['accra', 'ghana'],
        'Rwanda': ['kigali', 'rwanda'],
        'Tanzania': ['dar es salaam', 'tanzania'],
    }

    location = None
    for country, keywords in location_map.items():
        if any(kw in combined_text for kw in keywords):
            location = country
            break

    payment_map = {
        'M-Pesa': ['m-pesa', 'mpesa'],
        'UPI': ['upi'],
        'Stripe': ['stripe'],
        'PayPal': ['paypal'],
        'WeChat Pay': ['wechat pay', 'wechat'],
        'Alipay': ['alipay'],
        'PIX': ['pix'],
        'GCash': ['gcash'],
        'Paystack': ['paystack'],
        'Flutterwave': ['flutterwave'],
        'Bank Transfer': ['bank transfer'],
    }
    payment = None
    for method, keywords in payment_map.items():
        if any(kw in combined_text for kw in keywords):
            payment = method
            break

    communication = None
    if 'whatsapp' in combined_text:
        communication = 'WhatsApp'
    elif 'telegram' in combined_text:
        communication = 'Telegram'
    elif 'email' in combined_text:
        communication = 'Email'
    elif 'wechat' in combined_text:
        communication = 'WeChat'

    if location or payment or communication:
        cursor.execute(
            "INSERT INTO context_data (conversation_id, location, payment_method, communication_channel) VALUES (%s, %s, %s, %s)",
            (conversation_id, location, payment, communication)
        )

# ============================================
# QUALIFICATION CHECK — tightened triggers
# ============================================
def check_qualification(conversation_id, assistant_message, user_message, audit_contexts, cursor):
    qualified = False
    msg_lower = assistant_message.lower()
    user_lower = (user_message or '').lower()
    combined = msg_lower + ' ' + user_lower

    # Tightened triggers — must show clear purchase intent
    if 'book' in combined and 'call' in combined:
        qualified = True
    if 'calendly' in combined:
        qualified = True
    if 'eli' in combined and ('connect' in combined or 'talk' in combined or 'loop in' in combined):
        qualified = True
    if 'ready to start' in combined or "let's move forward" in combined or 'send me the link' in combined:
        qualified = True
    # Budget mentioned by USER (not just in assistant context)
    if re.search(r'\b(budget|willing to spend|i have|we have|allocated)\b', user_lower) and '$' in user_lower:
        qualified = True

    if qualified:
        cursor.execute(
            "SELECT id FROM leads WHERE conversation_id = %s AND notified_at IS NOT NULL",
            (conversation_id,)
        )
        already_notified = cursor.fetchone()

        if not already_notified:
            lead_data = extract_lead_data_from_history(conversation_id, cursor)

            if not lead_data.get('company') and 'tool3' in audit_contexts:
                lead_data['company'] = audit_contexts['tool3'].get('company_name', '')
            if not lead_data.get('industry') and 'tool3' in audit_contexts:
                lead_data['industry'] = audit_contexts['tool3'].get('industry', '')

            cursor.execute(
                "INSERT INTO leads (conversation_id, qualification_status, notified_at) VALUES (%s, %s, NOW()) RETURNING id",
                (conversation_id, 'qualified')
            )

            notify_in_background(notify_eli_qualified_lead, conversation_id, lead_data, audit_contexts)

# ============================================
# ADMIN STATS — protected
# ============================================
@app.route('/api/stats', methods=['GET'])
def stats():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as total FROM conversations")
        total_conversations = cur.fetchone()['total']

        cur.execute("SELECT COUNT(*) as total FROM conversations WHERE created_at >= CURRENT_DATE")
        conversations_today = cur.fetchone()['total']

        cur.execute("SELECT COUNT(*) as total FROM leads WHERE qualification_status = 'qualified'")
        qualified_leads = cur.fetchone()['total']

        cur.execute("SELECT COUNT(*) as total FROM messages")
        total_messages = cur.fetchone()['total']

        avg_messages = round(total_messages / total_conversations, 1) if total_conversations > 0 else 0

        cur.close()
        release_db_connection(conn)

        return jsonify({
            'total_conversations': total_conversations,
            'conversations_today': conversations_today,
            'qualified_leads': qualified_leads,
            'total_messages': total_messages,
            'avg_messages_per_conversation': avg_messages
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# ADMIN CONVERSATIONS — protected
# Read actual conversation content from admin
# ============================================
@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        limit = min(int(request.args.get('limit', 20)), 100)
        offset = int(request.args.get('offset', 0))

        cur.execute("""
            SELECT c.id, c.session_id, c.created_at, c.status, c.lead_quality_score,
                   COUNT(m.id) as message_count,
                   l.qualification_status, l.email, l.budget_range
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            LEFT JOIN leads l ON l.conversation_id = c.id
            GROUP BY c.id, l.qualification_status, l.email, l.budget_range
            ORDER BY c.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))

        conversations = cur.fetchall()

        result = []
        for conv in conversations:
            cur.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at",
                (conv['id'],)
            )
            messages = cur.fetchall()
            result.append({
                'id': conv['id'],
                'session_id': conv['session_id'],
                'created_at': conv['created_at'].isoformat(),
                'message_count': conv['message_count'],
                'qualification_status': conv['qualification_status'],
                'email': conv['email'],
                'budget_range': conv['budget_range'],
                'messages': [{'role': m['role'], 'content': m['content'], 'created_at': m['created_at'].isoformat()} for m in messages]
            })

        cur.close()
        release_db_connection(conn)
        return jsonify({'conversations': result, 'total': len(result), 'offset': offset})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# HEALTH CHECK — now verifies DB connection
# ============================================
@app.route('/api/health', methods=['GET'])
def health():
    db_status = 'unknown'
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        release_db_connection(conn)
        db_status = 'healthy'
    except Exception as e:
        db_status = f'error: {str(e)}'

    return jsonify({
        'status': 'healthy' if db_status == 'healthy' else 'degraded',
        'database': db_status,
        'timestamp': datetime.now().isoformat()
    })

# ============================================
# TEST ENDPOINTS
# ============================================
@app.route('/api/test-email', methods=['GET'])
@limiter.limit("5 per hour")
def test_email():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    sent = send_email_notification(
        subject='🧪 TEST EMAIL - LocalOS Nuru',
        body_text=f"✅ Resend API working.\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        body_html=f"<h2 style='color:#10b981;'>✅ Resend API Test Successful</h2><p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
    )
    return jsonify({'success': sent, 'timestamp': datetime.now().isoformat()})

@app.route('/api/test-whatsapp', methods=['GET'])
@limiter.limit("5 per hour")
def test_whatsapp():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    sent = send_whatsapp_notification(
        f"🧪 CallMeBot test — LocalOS Nuru\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"If you see this, WhatsApp notifications are working ✅"
    )
    return jsonify({
        'success': sent,
        'message': 'WhatsApp test sent ✅' if sent else 'WhatsApp test failed ❌ — check CALLMEBOT_API_KEY in Railway vars'
    })

# ============================================
# STARTUP
# ============================================
init_pool()
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)