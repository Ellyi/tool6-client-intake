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
import json
from urllib.parse import quote

load_dotenv()
from utils.model_router import get_model

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024

CORS(app, origins=["https://eliombogo.com", "https://www.eliombogo.com"])

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

ADMIN_SECRET = os.getenv('ADMIN_SECRET', '')

def require_admin_key():
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
# ============================================
ALLOWED_TAGS = []

def sanitize_input(text, max_length=2000):
    if not text:
        return ''
    text = bleach.clean(text, tags=ALLOWED_TAGS, strip=True)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = text[:max_length]
    return text.strip()

# ============================================
# CLAUDE CLIENT
# ============================================
claude_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# ============================================
# SYSTEM PROMPT
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
            return "You are Nuru, the intelligent client intake assistant for LocalOS. Qualify potential clients by understanding their business context."

SYSTEM_PROMPT = load_system_prompt()
print(f"✅ System prompt loaded ({len(SYSTEM_PROMPT)} characters)")

# ============================================
# AUDIT CONTEXT LOADER
# ============================================
_context_cache = {}

def load_audit_context(session_id):
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
# WHATSAPP
# ============================================
def send_whatsapp_notification(message):
    api_key = os.getenv('CALLMEBOT_API_KEY')
    phone = os.getenv('WHATSAPP_PHONE', '254701475000')
    if not api_key:
        return False
    try:
        encoded_message = quote(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded_message}&apikey={api_key}"
        response = requests.get(url, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ WhatsApp failed: {e}")
        return False

# ============================================
# EMAIL
# ============================================
def send_email_notification(subject, body_text, body_html=None, retries=2):
    resend_api_key = os.getenv('RESEND_API_KEY')
    notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
    from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')
    if not resend_api_key:
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
                return True
        except Exception as e:
            print(f"❌ Email attempt {attempt+1} failed: {e}")
    return False

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

    nuru_url = f"https://eliombogo.com/#nuru?session={result_data.get('session_id', '')}"
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
        return response.status_code in [200, 201]
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
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', user_email):
            return jsonify({'error': 'Invalid email format'}), 400

        result_data['session_id'] = session_id
        notify_in_background(send_tool_completion_email, user_email, tool_number, result_data)
        notify_in_background(send_whatsapp_notification,
            f"🔔 Tool #{tool_number} completed\nEmail: {user_email}\nSession: {session_id}"
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# INTELLIGENCE LAYER — VISITOR METADATA
# Captures origin, device, referrer on first message
# Non-blocking — runs in background thread
# ============================================

def detect_device_type(user_agent_str):
    """Parse User-Agent to determine device type"""
    if not user_agent_str:
        return 'unknown'
    ua = user_agent_str.lower()
    if any(x in ua for x in ['iphone', 'android', 'mobile', 'blackberry', 'windows phone']):
        return 'mobile'
    if any(x in ua for x in ['ipad', 'tablet', 'kindle']):
        return 'tablet'
    if any(x in ua for x in ['mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera']):
        return 'desktop'
    return 'unknown'

def detect_referrer_source(referrer_url):
    """Categorise referrer URL into traffic source"""
    if not referrer_url:
        return 'direct'
    url = referrer_url.lower()
    if 'linkedin.com' in url:
        return 'linkedin'
    if 'google.' in url:
        return 'google'
    if 'twitter.com' in url or 't.co' in url or 'x.com' in url:
        return 'twitter'
    if 'facebook.com' in url or 'fb.com' in url:
        return 'facebook'
    if 'whatsapp.com' in url or 'wa.me' in url:
        return 'whatsapp'
    if 'eliombogo.com' in url:
        # Internal navigation — which tool did they come from?
        if '/tool3' in url:
            return 'tool3'
        if '/tool4' in url:
            return 'tool4'
        if '/tool5' in url:
            return 'tool5'
        if '/blog' in url:
            return 'blog'
        return 'internal'
    if 'github.com' in url:
        return 'github'
    if 'reddit.com' in url:
        return 'reddit'
    return 'other'

def get_geo_from_ip(ip_address):
    """
    Lookup country/city from IP using ip-api.com (free, no key).
    Rate limit: 45 req/min — fine for our traffic.
    Returns dict or empty dict on failure.
    """
    if not ip_address or ip_address in ['127.0.0.1', 'localhost', '::1']:
        return {}
    try:
        response = requests.get(
            f"http://ip-api.com/json/{ip_address}?fields=country,city,regionName,status",
            timeout=3
        )
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                return {
                    'country': data.get('country', ''),
                    'city': data.get('city', ''),
                    'region': data.get('regionName', '')
                }
    except:
        pass
    return {}

def get_real_ip(req):
    """Extract real IP from request, handling proxies"""
    # Railway uses X-Forwarded-For
    forwarded = req.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return req.remote_addr or ''

def capture_visitor_metadata_async(conversation_id, ip_address, user_agent, referrer_url, entry_point):
    """
    Background thread: geo lookup + update conversation_intelligence.
    Chat response is never blocked waiting for this.
    """
    try:
        geo = get_geo_from_ip(ip_address)
        device_type = detect_device_type(user_agent)
        referrer_source = detect_referrer_source(referrer_url)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO conversation_intelligence
                (conversation_id, ip_country, ip_city, ip_region,
                 referrer_url, referrer_source, device_type, entry_point, total_turns)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (conversation_id) DO UPDATE SET
                ip_country = EXCLUDED.ip_country,
                ip_city = EXCLUDED.ip_city,
                ip_region = EXCLUDED.ip_region,
                referrer_url = EXCLUDED.referrer_url,
                referrer_source = EXCLUDED.referrer_source,
                device_type = EXCLUDED.device_type,
                entry_point = COALESCE(conversation_intelligence.entry_point, EXCLUDED.entry_point),
                updated_at = NOW()
        """, (
            conversation_id,
            geo.get('country', ''),
            geo.get('city', ''),
            geo.get('region', ''),
            referrer_url[:500] if referrer_url else '',
            referrer_source,
            device_type,
            entry_point
        ))
        conn.commit()
        cur.close()
        release_db_connection(conn)
        print(f"✅ Visitor metadata captured: {geo.get('country', 'unknown')} | {device_type} | {referrer_source}")
    except Exception as e:
        print(f"❌ Visitor metadata capture failed: {e}")

# ============================================
# INTELLIGENCE LAYER — SECURITY HONEYPOT
# Silent detection — attacker sees normal response.
# Eli sees the attempt logged in security_events.
# Protects: system prompt, API credits, competitive advantage.
# ============================================

# Injection patterns — what thieves and competitors try
INJECTION_PATTERNS = [
    r'ignore\s+(previous|all|your|the)\s+(instructions?|prompts?|rules?|directives?)',
    r'forget\s+(everything|all|your|the|what)',
    r'(repeat|reveal|show|tell\s+me|print|output|display)\s+(your\s+)?(system\s+)?(prompt|instructions?|rules?|context)',
    r'you\s+are\s+now\s+(a\s+)?(?!nuru)',
    r'act\s+as\s+(a\s+)?(?!nuru)',
    r'(new\s+)?jailbreak',
    r'pretend\s+(you\s+are|to\s+be)',
    r'(disable|bypass|override)\s+(safety|filter|restriction|rule)',
    r'(what|tell me)\s+(are|is)\s+your\s+(real\s+)?(instructions?|purpose|goal|training)',
    r'system\s+prompt\s*(:|=|\?)',
    r'<\s*system\s*>',
    r'\[\s*system\s*\]',
    r'(translate|convert)\s+your\s+(instructions?|prompt)',
    r'token\s+limit\s+bypass',
    r'context\s+window\s+(exploit|hack|bypass)',
]

# Competitor recon patterns — systematic probing of methodology
RECON_PATTERNS = [
    r'(what|which)\s+(AI|LLM|model|system)\s+(are\s+you|do\s+you\s+use|powers?\s+you)',
    r'(what|which)\s+(technology|tech\s+stack|infrastructure)',
    r'(how\s+do\s+you|how\s+does\s+this)\s+(work|detect|qualify|score)',
    r'(copy|replicate|recreate|clone|steal)\s+(this|your|the)\s+(system|approach|method)',
    r'(who|what)\s+(built|made|created|developed|coded)\s+(you|this|nuru)',
    r'what\s+is\s+your\s+(pricing|margin|markup|cost)',
]

def detect_injection_attempt(message):
    """
    Silently check for injection/recon attempts.
    Returns (is_suspicious, event_type, pattern_matched)
    """
    msg_lower = message.lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return True, 'injection_attempt', pattern

    # Check for systematic recon (less aggressive — log but don't flag hard)
    for pattern in RECON_PATTERNS:
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return True, 'competitor_recon', pattern

    return False, None, None

def log_security_event_async(conversation_id, event_type, message_content, ip_address, details):
    """Background: log security event without blocking chat response"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO security_events (conversation_id, event_type, message_content, ip_address, details)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            conversation_id,
            event_type,
            message_content[:500],
            ip_address,
            json.dumps(details)
        ))
        # Increment injection counter + flag conversation
        cur.execute("""
            UPDATE conversation_intelligence
            SET injection_attempts = injection_attempts + 1,
                flagged_suspicious = TRUE,
                updated_at = NOW()
            WHERE conversation_id = %s
        """, (conversation_id,))
        conn.commit()
        cur.close()
        release_db_connection(conn)
        # Alert Eli via WhatsApp for injection attempts (not recon — too noisy)
        if event_type == 'injection_attempt':
            notify_in_background(
                send_whatsapp_notification,
                f"⚠️ INJECTION ATTEMPT\n"
                f"Conv: {conversation_id}\n"
                f"IP: {ip_address}\n"
                f"Message: {message_content[:100]}"
            )
    except Exception as e:
        print(f"❌ Security event log failed: {e}")

# ============================================
# INTELLIGENCE LAYER — SIGNAL EXTRACTION
# Captures the vocabulary of pain in their own words.
# After 100 conversations, you'll know how your buyers
# describe their problems better than anyone on Earth.
# ============================================

# Pain signal phrases — exact expressions of business pain
PAIN_SIGNALS = [
    'takes forever', 'kills our', 'so slow', 'manual', 'repetitive',
    'copy paste', 'spreadsheet', 'keeps breaking', 'chase',
    'follow up', 'chasing', 'losing track', 'drowning', 'overwhelmed',
    'can\'t keep up', 'always late', 'behind', 'error prone', 'mistakes',
    'inconsistent', 'painful', 'nightmare', 'bottleneck', 'backlog',
    'taking too long', 'too much time', 'hours on', 'days on',
    'stuck', 'broken process', 'waste time', 'time consuming',
    'manually entering', 'reconcil', 'mismatch', 'wrong data',
    'approve everything', 'sign off', 'waiting for', 'approval process',
    'duplicate', 'data entry', 'copy the same', 'do it again',
    'no system', 'no visibility', 'can\'t track', 'don\'t know',
    'leakage', 'slipping through', 'fall through', 'miss',
]

# Competitor mentions
COMPETITOR_NAMES = [
    'zapier', 'make', 'integromat', 'n8n', 'chatgpt', 'openai', 'gpt',
    'upwork', 'fiverr', 'toptal', 'clutch', 'hubspot', 'salesforce',
    'monday', 'asana', 'notion', 'airtable', 'clickup',
    'accenture', 'deloitte', 'mckinsey', 'pwc', 'kpmg', 'ey',
    'power automate', 'microsoft flow', 'uipath', 'automation anywhere',
    'blue prism', 'workato', 'tray.io', 'boomi', 'mulesoft',
    'local agency', 'dev agency', 'software house', 'consultant',
]

# AI literacy signals
ZONE_1_SIGNALS = [
    'heard about it', 'not sure', 'just starting', "don't know", 'what is',
    'explain', 'new to', 'beginner', 'curious', 'never used', 'no idea',
    "haven't tried", 'sounds interesting', 'my friend said',
]
ZONE_2_SIGNALS = [
    'tried chatgpt', 'been looking', 'exploring', 'options', 'comparing',
    'seen demos', 'read about', 'heard good things', 'evaluating',
    'pilot', 'testing', 'poc', 'proof of concept',
]
ZONE_3_SIGNALS = [
    'api', 'dev team', 'built', 'integrated', 'failed', 'didn\'t work',
    'llm', 'fine-tune', 'fine-tuned', 'embedding', 'vector', 'rag',
    'langchain', 'workflow', 'deployed', 'production', 'architecture',
    'backend', 'frontend', 'endpoint', 'webhook', 'stack',
]

# Urgency signals
FAST_PATH_SIGNALS = [
    'urgent', 'asap', 'immediately', 'drowning', 'losing money',
    'crisis', 'now', 'today', 'this week', 'can\'t wait', 'emergency',
    'critical', 'bleeding', 'fire', 'deadline', 'yesterday',
]

def extract_pain_vocabulary(message):
    """Extract exact pain phrases from user message"""
    found = []
    msg_lower = message.lower()
    for signal in PAIN_SIGNALS:
        if signal in msg_lower:
            # Find the broader phrase context (±20 chars around match)
            idx = msg_lower.find(signal)
            start = max(0, idx - 20)
            end = min(len(message), idx + len(signal) + 20)
            phrase = message[start:end].strip()
            found.append(phrase)
    return found[:5]  # Cap at 5 per message

def detect_competitor_mentions(message):
    """Find competitor tools/agencies mentioned"""
    found = []
    msg_lower = message.lower()
    for name in COMPETITOR_NAMES:
        if name in msg_lower:
            found.append(name)
    return found

def detect_ai_literacy_zone(message):
    """Detect AI literacy zone 1/2/3 from message content"""
    msg_lower = message.lower()
    zone3_score = sum(1 for s in ZONE_3_SIGNALS if s in msg_lower)
    zone2_score = sum(1 for s in ZONE_2_SIGNALS if s in msg_lower)
    zone1_score = sum(1 for s in ZONE_1_SIGNALS if s in msg_lower)
    if zone3_score > 0:
        return 3
    if zone2_score > 0:
        return 2
    if zone1_score > 0:
        return 1
    return None  # Not detected yet

def detect_path_type(message):
    """Detect fast vs slow path from urgency signals"""
    msg_lower = message.lower()
    if any(s in msg_lower for s in FAST_PATH_SIGNALS):
        return 'fast_path'
    slow_signals = ['planning', 'exploring', 'q2', 'q3', 'q4', 'next year',
                    'eventually', 'thinking about', 'research', 'budget cycle']
    if any(s in msg_lower for s in slow_signals):
        return 'slow_path'
    return None

def extract_email_from_message(message):
    """Extract email address if user shares it"""
    match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', message)
    return match.group(0) if match else None

# ============================================
# INTELLIGENCE LAYER — AUTO SEGMENTATION
# Assigns a visitor segment based on conversation patterns.
# After N conversations, these segments reveal who your
# real buyers are — not who you think they are.
# ============================================

def auto_segment_visitor(conversation_id, industry, user_messages):
    """
    Assign visitor segment based on industry + conversation patterns.
    Format: '{industry}_{role}_{type}'
    Examples: 'logistics_ops_manager', 'healthcare_ceo_founder', 'unknown_it_technical'
    """
    all_text = ' '.join(user_messages).lower()

    # Role detection
    role = 'unknown'
    role_signals = {
        'ceo_founder': ['ceo', 'founder', 'owner', 'co-founder', 'started', 'my company', 'i built', 'i run'],
        'ops_manager': ['operations', 'ops', 'manager', 'managing', 'we manage', 'my team'],
        'it_technical': ['developer', 'engineer', 'technical', 'api', 'backend', 'stack', 'code', 'cto', 'it'],
        'finance': ['cfo', 'finance', 'accounting', 'financial', 'budget', 'cost'],
        'sales': ['sales', 'revenue', 'clients', 'leads', 'deals', 'pipeline', 'crm'],
        'hr': ['hr', 'hiring', 'recruitment', 'onboarding', 'people ops', 'human resources'],
    }
    for role_name, signals in role_signals.items():
        if any(s in all_text for s in signals):
            role = role_name
            break

    # Type detection
    visitor_type = 'unknown'
    if any(s in all_text for s in FAST_PATH_SIGNALS):
        visitor_type = 'urgent_buyer'
    elif any(s in all_text for s in ['planning', 'explore', 'researching', 'evaluating']):
        visitor_type = 'slow_evaluator'
    elif any(s in all_text for s in ['budget', 'cost', 'price', 'how much', 'afford']):
        visitor_type = 'price_focused'
    elif any(s in all_text for s in ['proof', 'case study', 'example', 'show me', 'evidence']):
        visitor_type = 'skeptic_needs_proof'
    elif any(s in all_text for s in ['learn', 'understand', 'curious', 'just looking', 'explore']):
        visitor_type = 'browser_learner'

    industry_slug = (industry or 'unknown').lower().replace(' ', '_').replace('&', 'and')
    return f"{industry_slug}_{role}_{visitor_type}"

# ============================================
# INTELLIGENCE LAYER — CIP PATTERN FEED
# Every conversation outcome feeds the learning brain.
# Runs in background after conversation completes/updates.
# ============================================

def upsert_cip_pattern(pattern_type, industry, visitor_segment, pattern_data):
    """
    Insert or increment a CIP pattern.
    Identical patterns increment count — reveals frequency.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        pattern_json = json.dumps(pattern_data, sort_keys=True)

        # Check if identical pattern exists
        cur.execute("""
            SELECT id FROM cip_patterns
            WHERE pattern_type = %s
              AND COALESCE(industry, '') = COALESCE(%s, '')
              AND COALESCE(visitor_segment, '') = COALESCE(%s, '')
              AND pattern_data::text = %s
        """, (pattern_type, industry, visitor_segment, pattern_json))

        existing = cur.fetchone()
        if existing:
            cur.execute("""
                UPDATE cip_patterns
                SET occurrence_count = occurrence_count + 1, last_seen = NOW()
                WHERE id = %s
            """, (existing['id'],))
        else:
            cur.execute("""
                INSERT INTO cip_patterns (pattern_type, industry, visitor_segment, pattern_data)
                VALUES (%s, %s, %s, %s)
            """, (pattern_type, industry, visitor_segment, json.dumps(pattern_data)))

        conn.commit()
        cur.close()
        release_db_connection(conn)
    except Exception as e:
        print(f"❌ CIP pattern update failed: {e}")

def feed_cip_engine_async(conversation_id, intel_record, outcome):
    """
    Background: extract patterns from completed conversation and feed CIP.
    This is what makes Nuru smarter every month.
    """
    try:
        industry = intel_record.get('industry_detected', 'unknown')
        segment = intel_record.get('visitor_segment', 'unknown')
        total_turns = intel_record.get('total_turns', 0)
        dropout_turn = intel_record.get('dropout_turn')
        path_type = intel_record.get('path_type', 'unknown')
        referrer = intel_record.get('referrer_source', 'unknown')
        device = intel_record.get('device_type', 'unknown')
        ai_zone = intel_record.get('ai_literacy_zone')
        had_tool_context = intel_record.get('entry_point') in ['tool3', 'tool4', 'tool5']
        competitors = intel_record.get('competitor_mentions', [])

        # Pattern: DROPOUT — when and where do people disengage?
        if outcome == 'bounced' and dropout_turn:
            upsert_cip_pattern('dropout', industry, segment, {
                'turn': dropout_turn,
                'path': path_type,
                'referrer': referrer,
                'device': device,
                'ai_zone': ai_zone,
            })

        # Pattern: CONVERSION — what does a converting conversation look like?
        if outcome in ['escalated', 'qualified', 'email_captured']:
            upsert_cip_pattern('conversion', industry, segment, {
                'turns_to_convert': total_turns,
                'trigger': outcome,
                'had_tool_context': had_tool_context,
                'path': path_type,
                'referrer': referrer,
                'device': device,
                'ai_zone': ai_zone,
            })

        # Pattern: COMPETITOR → PAIN — what competitor mention predicts conversion?
        for competitor in (competitors if isinstance(competitors, list) else []):
            if outcome in ['escalated', 'qualified']:
                upsert_cip_pattern('competitor_to_conversion', industry, segment, {
                    'competitor': competitor,
                    'outcome': outcome,
                })

        # Pattern: PATH OUTCOME — does fast/slow path predict outcome?
        if path_type:
            upsert_cip_pattern('path_outcome', industry, segment, {
                'path': path_type,
                'outcome': outcome,
                'turns': total_turns,
            })

        # Pattern: REFERRER QUALITY — which channel sends buyers vs browsers?
        upsert_cip_pattern('referrer_quality', industry, segment, {
            'referrer': referrer,
            'outcome': outcome,
        })

        print(f"✅ CIP engine fed: {outcome} | {industry} | {segment}")
    except Exception as e:
        print(f"❌ CIP engine feed failed: {e}")

# ============================================
# INTELLIGENCE LAYER — UPDATE CONVERSATION INTEL
# Called every chat turn. Builds the picture incrementally.
# ============================================

def update_conversation_intelligence_async(
    conversation_id, turn_number, user_message,
    industry=None, ai_zone=None, path_type=None,
    pain_vocab=None, competitors=None, segment=None, outcome=None
):
    """Background: update conversation_intelligence with per-turn signals"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get current state
        cur.execute("""
            SELECT pain_vocabulary, competitor_mentions, total_turns,
                   ai_literacy_zone, path_type, industry_detected,
                   visitor_segment, outcome
            FROM conversation_intelligence
            WHERE conversation_id = %s
        """, (conversation_id,))
        current = cur.fetchone()

        if not current:
            # Record doesn't exist yet (shouldn't happen but handle gracefully)
            cur.execute("""
                INSERT INTO conversation_intelligence (conversation_id, total_turns)
                VALUES (%s, %s)
                ON CONFLICT (conversation_id) DO NOTHING
            """, (conversation_id, turn_number))
            conn.commit()
            cur.close()
            release_db_connection(conn)
            return

        # Merge pain vocabulary (accumulate, don't overwrite)
        existing_pain = current['pain_vocabulary'] if current['pain_vocabulary'] else []
        if isinstance(existing_pain, str):
            existing_pain = json.loads(existing_pain)
        if pain_vocab:
            merged_pain = list(set(existing_pain + pain_vocab))[:20]  # Cap at 20
        else:
            merged_pain = existing_pain

        # Merge competitor mentions
        existing_competitors = current['competitor_mentions'] if current['competitor_mentions'] else []
        if isinstance(existing_competitors, str):
            existing_competitors = json.loads(existing_competitors)
        if competitors:
            merged_competitors = list(set(existing_competitors + competitors))
        else:
            merged_competitors = existing_competitors

        # Calculate avg message length (rolling average proxy — track in turns)
        avg_len = len(user_message) if user_message else 0

        # Set segment if newly detected and not already set
        final_segment = segment if segment else current.get('visitor_segment')
        final_zone = ai_zone if ai_zone else current.get('ai_literacy_zone')
        final_path = path_type if path_type else current.get('path_type')
        final_industry = industry if industry else current.get('industry_detected')
        final_outcome = outcome if outcome else current.get('outcome')

        cur.execute("""
            UPDATE conversation_intelligence SET
                total_turns = %s,
                pain_vocabulary = %s,
                competitor_mentions = %s,
                avg_message_length = %s,
                industry_detected = COALESCE(%s, industry_detected),
                ai_literacy_zone = COALESCE(%s, ai_literacy_zone),
                path_type = COALESCE(%s, path_type),
                visitor_segment = COALESCE(%s, visitor_segment),
                outcome = COALESCE(%s, outcome),
                updated_at = NOW()
            WHERE conversation_id = %s
        """, (
            turn_number,
            json.dumps(merged_pain),
            json.dumps(merged_competitors),
            avg_len,
            final_industry,
            final_zone,
            final_path,
            final_segment,
            final_outcome,
            conversation_id
        ))
        conn.commit()
        cur.close()
        release_db_connection(conn)
    except Exception as e:
        print(f"❌ Intelligence update failed: {e}")

def log_email_capture_async(conversation_id, email, turn_number, industry,
                             pain_summary, segment, ip_country, referrer_source,
                             capture_context='user_volunteered'):
    """Background: log email capture to email_captures table"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_captures
                (conversation_id, email, captured_at_turn, capture_context,
                 industry, pain_summary, visitor_segment, ip_country, referrer_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (conversation_id, email) DO NOTHING
        """, (
            conversation_id, email, turn_number, capture_context,
            industry, pain_summary[:300] if pain_summary else '',
            segment, ip_country, referrer_source
        ))
        # Update conversation_intelligence with captured email
        cur.execute("""
            UPDATE conversation_intelligence SET
                email_captured = %s,
                email_captured_at_turn = %s,
                outcome = CASE
                    WHEN outcome IS NULL OR outcome = 'bounced' THEN 'email_captured'
                    ELSE outcome
                END,
                updated_at = NOW()
            WHERE conversation_id = %s
        """, (email, turn_number, conversation_id))
        conn.commit()
        cur.close()
        release_db_connection(conn)
        print(f"✅ Email captured: {email} at turn {turn_number}")
    except Exception as e:
        print(f"❌ Email capture log failed: {e}")

# ============================================
# LEAD DATA EXTRACTION
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

    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_text)
    if email_match:
        lead_data['email'] = email_match.group(0)

    phone_match = re.search(r'(\+?\d[\d\s\-().]{7,}\d)', user_text)
    if phone_match:
        lead_data['phone'] = phone_match.group(0).strip()

    budget_pattern = re.search(
        r'(?:budget|willing to spend|have|allocated|set aside|approved)[^\d$]{0,20}\$?([\d,]+[kK]?)'
        r'|\b([\d,]+[kK]?)\s*(?:budget|to spend|available|to invest)',
        user_text, re.IGNORECASE
    )
    if budget_pattern:
        matched = budget_pattern.group(1) or budget_pattern.group(2)
        lead_data['budget'] = f"${matched.strip()}"

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

    user_messages = [m['content'] for m in messages if m['role'] == 'user']
    for msg in user_messages:
        if len(msg) > 20:
            lead_data['problem'] = msg[:300] + ('...' if len(msg) > 300 else '')
            break

    timeline_patterns = ['this week', 'this month', 'next month', 'asap', 'urgent',
                         'q1', 'q2', 'q3', 'q4', '2 weeks', '1 month', '3 months', '6 months']
    for pattern in timeline_patterns:
        if pattern in lower_text:
            lead_data['timeline'] = pattern
            break

    return lead_data

# ============================================
# NOTIFY ELI — QUALIFIED LEAD
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

            CREATE TABLE IF NOT EXISTS conversation_intelligence (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id) UNIQUE NOT NULL,
                ip_country VARCHAR(100),
                ip_city VARCHAR(100),
                ip_region VARCHAR(100),
                referrer_url TEXT,
                referrer_source VARCHAR(100),
                device_type VARCHAR(50),
                visitor_segment VARCHAR(150),
                ai_literacy_zone INTEGER,
                entry_point VARCHAR(100),
                path_type VARCHAR(50),
                total_turns INTEGER DEFAULT 0,
                dropout_turn INTEGER,
                avg_message_length FLOAT,
                pain_vocabulary JSONB DEFAULT '[]',
                competitor_mentions JSONB DEFAULT '[]',
                industry_detected VARCHAR(100),
                outcome VARCHAR(100),
                email_captured VARCHAR(255),
                email_captured_at_turn INTEGER,
                injection_attempts INTEGER DEFAULT 0,
                flagged_suspicious BOOLEAN DEFAULT FALSE,
                cip_processed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cip_patterns (
                id SERIAL PRIMARY KEY,
                pattern_type VARCHAR(100) NOT NULL,
                industry VARCHAR(100),
                visitor_segment VARCHAR(150),
                pattern_data JSONB NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS email_captures (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                email VARCHAR(255) NOT NULL,
                captured_at_turn INTEGER,
                capture_context VARCHAR(100),
                industry VARCHAR(100),
                pain_summary TEXT,
                visitor_segment VARCHAR(150),
                ip_country VARCHAR(100),
                referrer_source VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(conversation_id, email)
            );

            CREATE TABLE IF NOT EXISTS security_events (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                event_type VARCHAR(100) NOT NULL,
                message_content TEXT,
                ip_address VARCHAR(50),
                details JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON conversations(session_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_leads_conversation_id ON leads(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_leads_notified ON leads(notified_at);
            CREATE INDEX IF NOT EXISTS idx_conv_intel_conversation ON conversation_intelligence(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_conv_intel_country ON conversation_intelligence(ip_country);
            CREATE INDEX IF NOT EXISTS idx_conv_intel_segment ON conversation_intelligence(visitor_segment);
            CREATE INDEX IF NOT EXISTS idx_conv_intel_outcome ON conversation_intelligence(outcome);
            CREATE INDEX IF NOT EXISTS idx_conv_intel_flagged ON conversation_intelligence(flagged_suspicious);
            CREATE INDEX IF NOT EXISTS idx_cip_type ON cip_patterns(pattern_type);
            CREATE INDEX IF NOT EXISTS idx_cip_industry ON cip_patterns(industry);
            CREATE INDEX IF NOT EXISTS idx_email_captures_email ON email_captures(email);
            CREATE INDEX IF NOT EXISTS idx_email_captures_created ON email_captures(created_at);
            CREATE INDEX IF NOT EXISTS idx_security_type ON security_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_security_ip ON security_events(ip_address);
        """)
        conn.commit()
        cur.close()
        release_db_connection(conn)
        print("✅ Database tables and indexes ready")
    except Exception as e:
        print(f"DB init error: {e}")

# ============================================
# CONTEXT DETECTION (existing — preserved)
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
# QUALIFICATION CHECK
# ============================================
def check_qualification(conversation_id, assistant_message, user_message, audit_contexts, cursor):
    qualified = False
    msg_lower = assistant_message.lower()
    user_lower = (user_message or '').lower()
    combined = msg_lower + ' ' + user_lower

    if 'book' in combined and 'call' in combined:
        qualified = True
    if 'calendly' in combined:
        qualified = True
    if 'eli' in combined and ('connect' in combined or 'talk' in combined or 'loop in' in combined):
        qualified = True
    if 'ready to start' in combined or "let's move forward" in combined or 'send me the link' in combined:
        qualified = True
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

            # Update intelligence outcome
            notify_in_background(
                update_conversation_intelligence_async,
                conversation_id, 0, '', outcome='escalated'
            )

# ============================================
# LOAD CIP CONTEXT FOR NURU
# Inject relevant patterns into system prompt at runtime.
# This is how Nuru gets smarter every month automatically.
# ============================================
def load_cip_context_for_industry(industry):
    """
    Fetch top CIP patterns for a detected industry.
    Injected into Nuru's system at turn 1 if industry is known.
    """
    if not industry:
        return ''
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT pattern_type, pattern_data, occurrence_count, visitor_segment
            FROM cip_patterns
            WHERE LOWER(industry) = LOWER(%s)
              AND occurrence_count >= 2
            ORDER BY occurrence_count DESC
            LIMIT 8
        """, (industry,))
        patterns = cur.fetchall()
        cur.close()
        release_db_connection(conn)

        if not patterns:
            return ''

        lines = [f"\n[CIP INTELLIGENCE — {industry.upper()} PATTERNS]"]
        lines.append("Based on real conversations with businesses in this industry:\n")

        for p in patterns:
            ptype = p['pattern_type']
            data = p['pattern_data']
            count = p['occurrence_count']

            if ptype == 'dropout' and data.get('turn'):
                lines.append(f"• Visitors in this industry tend to disengage at turn {data['turn']} — keep momentum before then.")
            elif ptype == 'conversion' and data.get('turns_to_convert'):
                lines.append(f"• Converting visitors in this industry average {data['turns_to_convert']} turns to qualify.")
            elif ptype == 'competitor_to_conversion' and data.get('competitor'):
                lines.append(f"• Visitors who mention '{data['competitor']}' in this industry frequently convert — dig into why it failed for them.")
            elif ptype == 'path_outcome':
                lines.append(f"• {data.get('path', 'unknown').replace('_', ' ').title()} in this industry leads to '{data.get('outcome', 'unknown')}' outcomes.")

        lines.append(f"\nTotal data points: {sum(p['occurrence_count'] for p in patterns)} conversations analysed.")
        return '\n'.join(lines)

    except Exception as e:
        print(f"❌ CIP context load failed: {e}")
        return ''

# ============================================
# MAIN CHAT ENDPOINT — v2.0
# Intelligence Layer fully integrated.
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
        entry_point = sanitize_input(data.get('entry_point', ''), max_length=100)
        # entry_point sent by JS when quick-pick button clicked:
        # 'quick_pick_1', 'quick_pick_2', 'quick_pick_3', 'quick_pick_4'
        # or 'tool3', 'tool4', 'tool5' if arriving from audit tools

        if not user_message:
            return jsonify({'error': 'Message cannot be empty'}), 400

        if not session_id:
            session_id = str(uuid.uuid4())

        # Capture visitor metadata (referrer, device) for intelligence layer
        ip_address = get_real_ip(request)
        user_agent = request.headers.get('User-Agent', '')
        referrer_url = request.headers.get('Referer', '') or data.get('referrer', '')

        # ============================================
        # SECURITY HONEYPOT — silent check first
        # ============================================
        is_suspicious, event_type, pattern_matched = detect_injection_attempt(user_message)

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

        # Count turns
        cur.execute("SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = %s AND role = 'user'", (conversation_id,))
        turn_number = cur.fetchone()['cnt']

        # Session expiry
        cur.execute("SELECT created_at FROM conversations WHERE id = %s", (conversation_id,))
        conv_record = cur.fetchone()
        if conv_record and (datetime.now() - conv_record['created_at'].replace(tzinfo=None)) > timedelta(days=30):
            conn.commit()
            cur.close()
            release_db_connection(conn)
            return jsonify({'response': "It's been a while — let's start fresh. What are you working on?", 'session_id': str(uuid.uuid4())})

        # Log security event in background (attacker sees normal response)
        if is_suspicious:
            notify_in_background(
                log_security_event_async,
                conversation_id, event_type, user_message, ip_address,
                {'pattern': str(pattern_matched), 'turn': turn_number}
            )

        # ============================================
        # INTELLIGENCE EXTRACTION — per turn signals
        # ============================================
        pain_vocab = extract_pain_vocabulary(user_message)
        competitors = detect_competitor_mentions(user_message)
        ai_zone = detect_ai_literacy_zone(user_message)
        path_type = detect_path_type(user_message)
        email_in_message = extract_email_from_message(user_message)

        # Industry detection from existing keyword map
        industry_keywords = {
            'logistics': 'Logistics', 'transport': 'Transport', 'shipping': 'Logistics',
            'port': 'Logistics', 'freight': 'Logistics', 'clearing': 'Logistics',
            'legal': 'Legal', 'law firm': 'Legal', 'advocate': 'Legal',
            'healthcare': 'Healthcare', 'hospital': 'Healthcare', 'clinic': 'Healthcare',
            'finance': 'Finance', 'fintech': 'Fintech', 'banking': 'Finance',
            'retail': 'Retail', 'ecommerce': 'E-commerce',
            'saas': 'SaaS', 'software': 'Software', 'tech': 'Technology',
            'manufacturing': 'Manufacturing', 'real estate': 'Real Estate',
            'education': 'Education', 'consulting': 'Consulting', 'agency': 'Agency',
        }
        detected_industry = None
        msg_lower = user_message.lower()
        for kw, ind in industry_keywords.items():
            if kw in msg_lower:
                detected_industry = ind
                break

        # Auto-segment at turn 4+ (enough data by then)
        visitor_segment = None
        if turn_number >= 4:
            cur.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' ORDER BY created_at",
                (conversation_id,)
            )
            all_user_msgs = [m['content'] for m in cur.fetchall()]
            cur.execute(
                "SELECT industry_detected FROM conversation_intelligence WHERE conversation_id = %s",
                (conversation_id,)
            )
            intel_row = cur.fetchone()
            known_industry = (intel_row['industry_detected'] if intel_row else None) or detected_industry
            visitor_segment = auto_segment_visitor(conversation_id, known_industry, all_user_msgs)

        # ============================================
        # FIRST MESSAGE: trigger visitor metadata capture
        # ============================================
        if turn_number == 1:
            notify_in_background(
                capture_visitor_metadata_async,
                conversation_id, ip_address, user_agent, referrer_url, entry_point or 'direct'
            )
        else:
            # Update intelligence record with per-turn signals
            notify_in_background(
                update_conversation_intelligence_async,
                conversation_id, turn_number, user_message,
                detected_industry, ai_zone, path_type,
                pain_vocab, competitors, visitor_segment
            )

        # Handle email capture if user shared their email
        if email_in_message:
            cur.execute(
                "SELECT industry_detected, visitor_segment, ip_country, referrer_source FROM conversation_intelligence WHERE conversation_id = %s",
                (conversation_id,)
            )
            intel = cur.fetchone()
            # Get first user message as pain summary
            cur.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' ORDER BY created_at LIMIT 1",
                (conversation_id,)
            )
            first_msg = cur.fetchone()
            pain_summary = first_msg['content'] if first_msg else ''

            notify_in_background(
                log_email_capture_async,
                conversation_id, email_in_message, turn_number,
                (intel['industry_detected'] if intel else detected_industry),
                pain_summary,
                (intel['visitor_segment'] if intel else visitor_segment),
                (intel['ip_country'] if intel else ''),
                (intel['referrer_source'] if intel else ''),
                'user_volunteered'
            )

        # ============================================
        # BUILD MESSAGE HISTORY FOR CLAUDE
        # ============================================
        cur.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,)
        )
        history = cur.fetchall()

        messages = []

        if len(history) == 1:
            # First message — inject all context available
            context_message = ''

            # Audit tool context
            if audit_contexts:
                context_message += "[AUDIT CONTEXT AVAILABLE]\n"
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
                if 'tool5' in audit_contexts:
                    ctx = audit_contexts['tool5']
                    context_message += f"\nTool #5 ROI: ${ctx.get('annual_savings'):,} annual savings"

            # CIP intelligence — inject relevant patterns if industry known
            if detected_industry:
                cip_context = load_cip_context_for_industry(detected_industry)
                if cip_context:
                    context_message += cip_context

            # Entry point context
            if entry_point:
                context_message += f"\n\n[ENTRY POINT: {entry_point}]"

            # Visitor intelligence hint (device + referrer for tone calibration)
            device = detect_device_type(user_agent)
            referrer_source = detect_referrer_source(referrer_url)
            context_message += f"\n[VISITOR CONTEXT: device={device}, referrer={referrer_source}]"

            if context_message:
                messages.append({"role": "user", "content": context_message + f"\n\nUser's first message: {user_message}"})
            else:
                messages.append({"role": "user", "content": user_message})
        else:
            # Subsequent turns — inject CIP context hint at turn 5 if industry detected
            for msg in history:
                messages.append({"role": msg['role'], "content": msg['content']})

            # At turn 5 — inject mid-conversation email capture instruction if not captured
            if turn_number == 5:
                cur.execute(
                    "SELECT email_captured FROM conversation_intelligence WHERE conversation_id = %s",
                    (conversation_id,)
                )
                intel_check = cur.fetchone()
                if not intel_check or not intel_check.get('email_captured'):
                    # Inject subtle instruction for Nuru to ask for email naturally
                    messages[-1]['content'] += "\n\n[SYSTEM HINT: If this is a substantive conversation, naturally offer to send them a personalised waste map by email. Only ask once. Don't be pushy.]"

        # ============================================
        # CALL CLAUDE
        # ============================================
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

        # Check if Nuru asked for email in this response (mid-conversation capture)
        if email_in_message is None and 'email' in assistant_message.lower():
            # Nuru asked for email — mark in intelligence as pending capture
            notify_in_background(
                update_conversation_intelligence_async,
                conversation_id, turn_number, assistant_message,
                outcome='email_capture_pending'
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
# INTELLIGENCE SUMMARY ENDPOINT
# The dashboard showing the moat growing in real time.
# Protected — admin only.
# ============================================
@app.route('/api/intelligence/summary', methods=['GET'])
def intelligence_summary():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Total conversations
        cur.execute("SELECT COUNT(*) as total FROM conversation_intelligence")
        total = cur.fetchone()['total']

        # Breakdown by country
        cur.execute("""
            SELECT ip_country, COUNT(*) as count
            FROM conversation_intelligence
            WHERE ip_country IS NOT NULL AND ip_country != ''
            GROUP BY ip_country ORDER BY count DESC LIMIT 10
        """)
        by_country = [dict(r) for r in cur.fetchall()]

        # Breakdown by referrer source
        cur.execute("""
            SELECT referrer_source, COUNT(*) as count
            FROM conversation_intelligence
            WHERE referrer_source IS NOT NULL
            GROUP BY referrer_source ORDER BY count DESC
        """)
        by_referrer = [dict(r) for r in cur.fetchall()]

        # Breakdown by device
        cur.execute("""
            SELECT device_type, COUNT(*) as count
            FROM conversation_intelligence
            WHERE device_type IS NOT NULL
            GROUP BY device_type ORDER BY count DESC
        """)
        by_device = [dict(r) for r in cur.fetchall()]

        # Outcome breakdown
        cur.execute("""
            SELECT outcome, COUNT(*) as count
            FROM conversation_intelligence
            WHERE outcome IS NOT NULL
            GROUP BY outcome ORDER BY count DESC
        """)
        by_outcome = [dict(r) for r in cur.fetchall()]

        # Top visitor segments
        cur.execute("""
            SELECT visitor_segment, COUNT(*) as count
            FROM conversation_intelligence
            WHERE visitor_segment IS NOT NULL
            GROUP BY visitor_segment ORDER BY count DESC LIMIT 10
        """)
        by_segment = [dict(r) for r in cur.fetchall()]

        # Top industries detected
        cur.execute("""
            SELECT industry_detected, COUNT(*) as count
            FROM conversation_intelligence
            WHERE industry_detected IS NOT NULL AND industry_detected != ''
            GROUP BY industry_detected ORDER BY count DESC
        """)
        by_industry = [dict(r) for r in cur.fetchall()]

        # Average dropout turn
        cur.execute("""
            SELECT AVG(dropout_turn) as avg_dropout, AVG(total_turns) as avg_turns
            FROM conversation_intelligence
            WHERE total_turns > 0
        """)
        turn_stats = cur.fetchone()

        # Email captures total
        cur.execute("SELECT COUNT(*) as total FROM email_captures")
        email_total = cur.fetchone()['total']

        # Security events
        cur.execute("""
            SELECT event_type, COUNT(*) as count
            FROM security_events
            GROUP BY event_type ORDER BY count DESC
        """)
        security_summary = [dict(r) for r in cur.fetchall()]

        # Pain vocabulary — most common phrases across all conversations
        cur.execute("""
            SELECT jsonb_array_elements_text(pain_vocabulary) as phrase, COUNT(*) as freq
            FROM conversation_intelligence
            WHERE pain_vocabulary != '[]'
            GROUP BY phrase ORDER BY freq DESC LIMIT 20
        """)
        top_pain_phrases = [dict(r) for r in cur.fetchall()]

        # Competitor mentions
        cur.execute("""
            SELECT jsonb_array_elements_text(competitor_mentions) as competitor, COUNT(*) as mentions
            FROM conversation_intelligence
            WHERE competitor_mentions != '[]'
            GROUP BY competitor ORDER BY mentions DESC LIMIT 10
        """)
        top_competitors = [dict(r) for r in cur.fetchall()]

        cur.close()
        release_db_connection(conn)

        return jsonify({
            'total_conversations_analysed': total,
            'email_captures': email_total,
            'by_country': by_country,
            'by_referrer_source': by_referrer,
            'by_device': by_device,
            'by_outcome': by_outcome,
            'top_visitor_segments': by_segment,
            'top_industries': by_industry,
            'turn_stats': {
                'avg_dropout_turn': round(float(turn_stats['avg_dropout'] or 0), 1),
                'avg_total_turns': round(float(turn_stats['avg_turns'] or 0), 1),
            },
            'top_pain_vocabulary': top_pain_phrases,
            'top_competitor_mentions': top_competitors,
            'security_events': security_summary,
            'generated_at': datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# CIP PATTERNS ENDPOINT
# What Nuru has learned. Gets smarter every month.
# ============================================
@app.route('/api/cip/patterns', methods=['GET'])
def cip_patterns():
    authorized, error = require_admin_key()
    if not authorized:
        return jsonify({'error': error}), 401

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        pattern_type = request.args.get('type')
        industry = request.args.get('industry')

        query = """
            SELECT id, pattern_type, industry, visitor_segment,
                   pattern_data, occurrence_count, last_seen, created_at
            FROM cip_patterns
            WHERE 1=1
        """
        params = []
        if pattern_type:
            query += " AND pattern_type = %s"
            params.append(pattern_type)
        if industry:
            query += " AND LOWER(industry) = LOWER(%s)"
            params.append(industry)

        query += " ORDER BY occurrence_count DESC LIMIT 50"
        cur.execute(query, params)
        patterns = cur.fetchall()

        # Summary stats
        cur.execute("""
            SELECT pattern_type, COUNT(*) as unique_patterns, SUM(occurrence_count) as total_occurrences
            FROM cip_patterns GROUP BY pattern_type ORDER BY total_occurrences DESC
        """)
        summary = [dict(r) for r in cur.fetchall()]

        cur.close()
        release_db_connection(conn)

        return jsonify({
            'patterns': [dict(p) for p in patterns],
            'summary': summary,
            'total_pattern_types': len(summary)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# ADMIN STATS
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

        cur.execute("SELECT COUNT(*) as total FROM email_captures")
        email_captures = cur.fetchone()['total']

        cur.execute("SELECT COUNT(*) as total FROM security_events WHERE event_type = 'injection_attempt'")
        injection_attempts = cur.fetchone()['total']

        avg_messages = round(total_messages / total_conversations, 1) if total_conversations > 0 else 0

        cur.close()
        release_db_connection(conn)

        return jsonify({
            'total_conversations': total_conversations,
            'conversations_today': conversations_today,
            'qualified_leads': qualified_leads,
            'total_messages': total_messages,
            'avg_messages_per_conversation': avg_messages,
            'email_captures': email_captures,
            'injection_attempts_blocked': injection_attempts,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# ADMIN CONVERSATIONS
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
                   l.qualification_status, l.email, l.budget_range,
                   ci.ip_country, ci.referrer_source, ci.device_type,
                   ci.visitor_segment, ci.outcome, ci.injection_attempts,
                   ci.flagged_suspicious, ci.email_captured
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            LEFT JOIN leads l ON l.conversation_id = c.id
            LEFT JOIN conversation_intelligence ci ON ci.conversation_id = c.id
            GROUP BY c.id, l.qualification_status, l.email, l.budget_range,
                     ci.ip_country, ci.referrer_source, ci.device_type,
                     ci.visitor_segment, ci.outcome, ci.injection_attempts,
                     ci.flagged_suspicious, ci.email_captured
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
                'email': conv['email'] or conv['email_captured'],
                'budget_range': conv['budget_range'],
                'country': conv['ip_country'],
                'referrer': conv['referrer_source'],
                'device': conv['device_type'],
                'segment': conv['visitor_segment'],
                'outcome': conv['outcome'],
                'flagged': conv['flagged_suspicious'],
                'injection_attempts': conv['injection_attempts'],
                'messages': [{'role': m['role'], 'content': m['content'], 'created_at': m['created_at'].isoformat()} for m in messages]
            })

        cur.close()
        release_db_connection(conn)
        return jsonify({'conversations': result, 'total': len(result), 'offset': offset})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# HEALTH CHECK
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
        f"Intelligence Layer v2.0 active ✅"
    )
    return jsonify({
        'success': sent,
        'message': 'WhatsApp test sent ✅' if sent else 'Failed ❌ — check CALLMEBOT_API_KEY'
    })

# ============================================
# STARTUP
# ============================================
init_pool()
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)