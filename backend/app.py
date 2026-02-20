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
import re

load_dotenv()
from utils.model_router import get_model

app = Flask(__name__)
CORS(app, origins=["https://eliombogo.com", "https://www.eliombogo.com"])

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

# Load system prompt from file
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
print(f"System prompt loaded ({len(SYSTEM_PROMPT)} characters)")

# Load context from Tools #3, #4, #5
def load_audit_context(session_id):
    contexts = {}
    
    try:
        response = requests.get(
            f'https://tool3-business-intel-backend-production.up.railway.app/api/session/{session_id}',
            timeout=3
        )
        if response.status_code == 200:
            contexts['tool3'] = response.json()
    except:
        pass
    
    try:
        response = requests.get(
            f'https://tool4-ai-readiness-production.up.railway.app/api/session/{session_id}',
            timeout=3
        )
        if response.status_code == 200:
            contexts['tool4'] = response.json()
    except:
        pass
    
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


# ============================================
# EMAIL NOTIFICATION VIA RESEND API
# ============================================

def send_email_notification(subject, body_text, body_html=None):
    """Send email via Resend API (HTTPS - works on Railway)"""
    resend_api_key = os.getenv('RESEND_API_KEY')
    notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
    from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')
    
    if not resend_api_key:
        print("WARNING: RESEND_API_KEY not set - email notification skipped")
        return False
    
    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': from_email,
                'to': [notify_email],
                'subject': subject,
                'html': body_html if body_html else body_text.replace('\n', '<br>'),
                'text': body_text
            },
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            response_data = response.json()
            print(f"✅ Email sent via Resend to {notify_email} - ID: {response_data.get('id', 'unknown')}")
            return True
        else:
            print(f"❌ Resend API error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Email send failed: {e}")
        return False


# ============================================
# EXTRACT LEAD DATA FROM CONVERSATION HISTORY
# Issue 5 fix: read actual messages before building lead_data
# ============================================

def extract_lead_data_from_history(conversation_id, cursor):
    """
    Parse full conversation history to extract company, industry,
    email, budget, and problem before sending escalation email.
    Returns populated lead_data dict instead of empty placeholders.
    """
    cursor.execute(
        "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
        (conversation_id,)
    )
    messages = cursor.fetchall()
    
    # Concatenate all user messages for extraction
    user_text = ' '.join([m['content'] for m in messages if m['role'] == 'user'])
    all_text = ' '.join([m['content'] for m in messages])
    
    lead_data = {}

    # Extract email
    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_text)
    if email_match:
        lead_data['email'] = email_match.group(0)

    # Extract phone (international formats)
    phone_match = re.search(r'(\+?\d[\d\s\-().]{7,}\d)', user_text)
    if phone_match:
        lead_data['phone'] = phone_match.group(0).strip()

    # Extract budget (any $ amount or budget mention with number)
    budget_match = re.search(r'\$[\d,]+[kK]?|\b(\d+[kK])\s*(budget|dollars?|USD)|\bbudget\s*(of|is|around|about)?\s*\$?[\d,]+[kK]?', user_text, re.IGNORECASE)
    if budget_match:
        lead_data['budget'] = budget_match.group(0).strip()
    
    # Extract company name - look for patterns like "my company is X", "we're at X", "I work at X"
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

    # Extract industry from common keywords
    industry_keywords = {
        'logistics': 'Logistics', 'transport': 'Transport', 'shipping': 'Logistics',
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

    # Extract problem summary — first user message is usually the clearest statement
    user_messages = [m['content'] for m in messages if m['role'] == 'user']
    if user_messages:
        # Use first substantive user message (>20 chars) as problem description
        for msg in user_messages:
            if len(msg) > 20:
                lead_data['problem'] = msg[:300] + ('...' if len(msg) > 300 else '')
                break

    # Extract timeline signals
    timeline_patterns = ['this week', 'this month', 'next month', 'asap', 'urgent',
                        'q1', 'q2', 'q3', 'q4', '2 weeks', '1 month', '3 months', '6 months']
    for pattern in timeline_patterns:
        if pattern in lower_text:
            lead_data['timeline'] = pattern
            break

    return lead_data


# ============================================
# NOTIFY ELI - QUALIFIED LEAD
# ============================================

def notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts):
    """Notify Eli via email when qualified lead detected"""
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
            body += f"""
Tool #3 Waste Score: {ctx.get('waste_score')}/100
Top Waste Zone: {ctx['top_waste_zones'][0]['name'] if ctx.get('top_waste_zones') else 'N/A'}
Hours Wasted: {ctx.get('total_hours_wasted')}/month"""

        if 'tool4' in audit_contexts:
            ctx = audit_contexts['tool4']
            body += f"""
Tool #4 Readiness: {ctx.get('readiness_score')}/100"""

        if 'tool5' in audit_contexts:
            ctx = audit_contexts['tool5']
            savings = ctx.get('annual_savings', 0)
            body += f"""
Tool #5 ROI: ${savings:,} annual savings"""

        body += f"""

CONVERSATION ID: {conversation_id}
TIME: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

ACTION: Reply to contact directly to book discovery call.
WhatsApp: +254 701 475 000
Calendly: https://calendly.com/eli-eliombogo/discovery-call
"""

        html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
  <div style="background: #1a2332; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
    <h2 style="margin: 0; color: #10b981;">🎯 Qualified Lead - LocalOS</h2>
    <p style="margin: 5px 0 0; color: #9ca3af; font-size: 14px;">{datetime.now().strftime('%B %d, %Y at %H:%M UTC')}</p>
  </div>
  
  <div style="background: #f9fafb; padding: 20px; border: 1px solid #e5e7eb;">
    <h3 style="color: #1a2332; border-bottom: 2px solid #10b981; padding-bottom: 8px;">Lead Details</h3>
    <p><strong>Company:</strong> {lead_data.get('company', 'Not captured yet')}</p>
    <p><strong>Industry:</strong> {lead_data.get('industry', 'Not captured yet')}</p>
    <p><strong>Contact:</strong> {lead_data.get('email', 'Not captured yet')}</p>
    <p><strong>Phone:</strong> {lead_data.get('phone', 'Not captured yet')}</p>
    <p><strong>Budget:</strong> {lead_data.get('budget', 'Mentioned in conversation')}</p>
    <p><strong>Timeline:</strong> {lead_data.get('timeline', 'Not stated')}</p>
    <p><strong>Problem:</strong> {lead_data.get('problem', 'See conversation')}</p>
  </div>"""

        if audit_contexts:
            html += """
  <div style="background: white; padding: 20px; border: 1px solid #e5e7eb; border-top: none;">
    <h3 style="color: #1a2332; border-bottom: 2px solid #10b981; padding-bottom: 8px;">Audit Data</h3>"""
            
            if 'tool3' in audit_contexts:
                ctx = audit_contexts['tool3']
                score = ctx.get('waste_score', 0)
                color = '#ef4444' if score >= 70 else '#f59e0b' if score >= 40 else '#10b981'
                html += f"""
    <p><strong>Waste Score:</strong> <span style="color: {color}; font-size: 18px; font-weight: bold;">{score}/100</span></p>
    <p><strong>Hours Wasted/Month:</strong> {ctx.get('total_hours_wasted', 'N/A')}</p>"""
            
            if 'tool5' in audit_contexts:
                ctx = audit_contexts['tool5']
                savings = ctx.get('annual_savings', 0)
                html += f"""
    <p><strong>Projected Annual Savings:</strong> <span style="color: #10b981; font-weight: bold;">${savings:,}</span></p>"""
            
            html += "</div>"

        html += f"""
  <div style="background: #1a2332; padding: 20px; border-radius: 0 0 8px 8px; text-align: center;">
    <a href="https://calendly.com/eli-eliombogo/discovery-call" 
       style="background: #10b981; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold; display: inline-block;">
      Book Discovery Call
    </a>
    <p style="color: #9ca3af; font-size: 12px; margin-top: 12px;">
      Conversation ID: {conversation_id} | Nuru - LocalOS AI
    </p>
  </div>
</div>"""

        email_sent = send_email_notification(
            subject=f"🎯 Qualified Lead - LocalOS | Conversation {conversation_id}",
            body_text=body,
            body_html=html
        )

        webhook_sent = False
        try:
            webhook_url = "https://script.google.com/macros/s/AKfycbw_DUBZMbh47xMP5Lg83Q04o66oDQFwdO6qM7pixoN4BzVLkR9iz4EiT2WrPU2NTAANlw/exec"
            webhook_response = requests.post(
                webhook_url,
                json={
                    'type': 'qualified_lead',
                    'timestamp': datetime.now().isoformat(),
                    'message': body,
                    'lead_data': lead_data,
                    'conversation_id': str(conversation_id)
                },
                timeout=5
            )
            webhook_sent = webhook_response.status_code == 200
        except Exception as webhook_error:
            print(f"Webhook failed: {webhook_error}")

        print(f"Eli notified of qualified lead (conversation {conversation_id}) - Email: {email_sent}, Webhook: {webhook_sent}")

    except Exception as e:
        print(f"Failed to notify Eli: {e}")


# ============================================
# DATABASE INIT - SAFE (no DROP tables)
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
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("Database tables ready")
    except Exception as e:
        print(f"DB init: {e}")

init_db()


# ============================================
# CHAT ENDPOINT
# ============================================

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')
        session_id = data.get('session_id')
        
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
            
            messages.append({
                "role": "user",
                "content": context_message + f"\n\nUser's first message: {user_message}"
            })
        else:
            for msg in history:
                messages.append({
                    "role": msg['role'],
                    "content": msg['content']
                })
        
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
        conn.close()
        
        return jsonify({
            'response': assistant_message,
            'session_id': session_id
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================
# CONTEXT DETECTION
# ============================================

def detect_and_save_context(conversation_id, user_msg, assistant_msg, cursor):
    combined_text = (user_msg + " " + assistant_msg).lower()
    
    location = None
    if 'nairobi' in combined_text or 'kenya' in combined_text:
        location = 'Kenya'
    elif 'lagos' in combined_text or 'nigeria' in combined_text:
        location = 'Nigeria'
    elif 'johannesburg' in combined_text or 'cape town' in combined_text or 'south africa' in combined_text:
        location = 'South Africa'
    elif 'cairo' in combined_text or 'egypt' in combined_text:
        location = 'Egypt'
    elif 'mumbai' in combined_text or 'india' in combined_text or 'delhi' in combined_text:
        location = 'India'
    elif 'beijing' in combined_text or 'shanghai' in combined_text or 'china' in combined_text:
        location = 'China'
    elif 'singapore' in combined_text:
        location = 'Singapore'
    elif 'manila' in combined_text or 'philippines' in combined_text:
        location = 'Philippines'
    elif 'dubai' in combined_text or 'abu dhabi' in combined_text or 'uae' in combined_text:
        location = 'UAE'
    elif 'new york' in combined_text or 'los angeles' in combined_text or 'chicago' in combined_text or 'san francisco' in combined_text or 'usa' in combined_text or 'united states' in combined_text or 'colorado' in combined_text:
        location = 'USA'
    elif 'toronto' in combined_text or 'vancouver' in combined_text or 'canada' in combined_text:
        location = 'Canada'
    elif 'london' in combined_text or 'manchester' in combined_text or 'uk' in combined_text or 'united kingdom' in combined_text:
        location = 'UK'
    elif 'berlin' in combined_text or 'munich' in combined_text or 'germany' in combined_text:
        location = 'Germany'
    elif 'paris' in combined_text or 'france' in combined_text:
        location = 'France'
    elif 'sydney' in combined_text or 'melbourne' in combined_text or 'australia' in combined_text:
        location = 'Australia'

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
    elif 'bank transfer' in combined_text:
        payment = 'Bank Transfer'

    communication = None
    if 'whatsapp' in combined_text:
        communication = 'WhatsApp'
    elif 'email' in combined_text:
        communication = 'Email'
    elif 'wechat' in combined_text:
        communication = 'WeChat'

    if location or payment or communication:
        cursor.execute(
            """INSERT INTO context_data 
               (conversation_id, location, payment_method, communication_channel)
               VALUES (%s, %s, %s, %s)""",
            (conversation_id, location, payment, communication)
        )


# ============================================
# QUALIFICATION CHECK
# Issue 5 fix: extract lead data from history before escalating
# ============================================

def check_qualification(conversation_id, assistant_message, user_message, audit_contexts, cursor):
    qualified = False

    msg_lower = assistant_message.lower()
    user_lower = (user_message or '').lower()
    combined = msg_lower + ' ' + user_lower

    if '$' in combined or 'budget' in combined:
        qualified = True

    if 'book' in combined and 'call' in combined:
        qualified = True

    if 'eli' in combined and ('connect' in combined or 'talk' in combined or 'discuss' in combined):
        qualified = True

    if 'ready to' in combined or "let's start" in combined or 'move forward' in combined:
        qualified = True

    if qualified:
        cursor.execute(
            "SELECT id FROM leads WHERE conversation_id = %s AND notified_at IS NOT NULL",
            (conversation_id,)
        )
        already_notified = cursor.fetchone()

        if not already_notified:
            # FIX: Extract real lead data from conversation history before escalating
            # Previously: lead_data was always empty {}, so email showed "Not provided" for everything
            lead_data = extract_lead_data_from_history(conversation_id, cursor)

            # Also pull company/industry from audit context if not found in conversation
            if not lead_data.get('company') and 'tool3' in audit_contexts:
                lead_data['company'] = audit_contexts['tool3'].get('company_name', '')
            if not lead_data.get('industry') and 'tool3' in audit_contexts:
                lead_data['industry'] = audit_contexts['tool3'].get('industry', '')

            cursor.execute(
                """INSERT INTO leads (conversation_id, qualification_status, notified_at)
                   VALUES (%s, %s, NOW())
                   RETURNING id""",
                (conversation_id, 'qualified')
            )

            notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts)


# ============================================
# ADMIN STATS ENDPOINT
# Issue 4 fix: was hardcoded to 0 in admin dashboard
# ============================================

@app.route('/api/stats', methods=['GET'])
def stats():
    """Admin stats endpoint - returns Nuru conversation and lead counts"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as total FROM conversations")
        total_conversations = cur.fetchone()['total']

        cur.execute("""
            SELECT COUNT(*) as total FROM conversations
            WHERE created_at >= CURRENT_DATE
        """)
        conversations_today = cur.fetchone()['total']

        cur.execute("""
            SELECT COUNT(*) as total FROM leads
            WHERE qualification_status = 'qualified'
        """)
        qualified_leads = cur.fetchone()['total']

        cur.execute("SELECT COUNT(*) as total FROM messages")
        total_messages = cur.fetchone()['total']

        avg_messages = round(total_messages / total_conversations, 1) if total_conversations > 0 else 0

        cur.close()
        conn.close()

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
# HEALTH CHECK
# ============================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


# ============================================
# TEST EMAIL ENDPOINT (FOR DEBUGGING)
# ============================================

@app.route('/api/test-email', methods=['GET'])
def test_email():
    """Test endpoint to verify Resend API works"""
    try:
        print("🧪 TEST EMAIL - Attempting to send via Resend...")
        
        resend_api_key = os.getenv('RESEND_API_KEY')
        notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
        from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')
        
        if not resend_api_key:
            return jsonify({
                'success': False,
                'error': 'RESEND_API_KEY not configured',
                'has_api_key': False
            }), 500
        
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': from_email,
                'to': [notify_email],
                'subject': '🧪 TEST EMAIL - LocalOS Nuru',
                'html': f"""
                <div style="font-family: Arial, sans-serif;">
                    <h2 style="color: #10b981;">✅ Resend API Test Successful</h2>
                    <p>This is a test email from Nuru backend via Resend.</p>
                    <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
                    <p><strong>Backend:</strong> Railway tool6-client-intake-production</p>
                    <p><strong>From:</strong> {from_email}</p>
                    <p><strong>To:</strong> {notify_email}</p>
                    <p style="color: #10b981; font-weight: bold;">If you receive this, Resend is working correctly!</p>
                </div>
                """,
                'text': f"✅ Resend API Test\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\nFrom: {from_email}\nTo: {notify_email}"
            },
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            response_data = response.json()
            return jsonify({
                'success': True,
                'message': f'Test email sent to {notify_email}',
                'email_id': response_data.get('id'),
                'timestamp': datetime.now().isoformat()
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'Resend API request failed',
                'status_code': response.status_code,
                'details': response.text
            }), response.status_code
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)