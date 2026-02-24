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
from urllib.parse import quote  # NEW: for CallMeBot URL encoding

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
# WHATSAPP NOTIFICATION VIA CALLMEBOT (FREE)
# NEW: Zero-cost WhatsApp alerts to Eli
# ============================================
#
# ONE-TIME SETUP (do this before deploying):
# 1. Save +34 644 59 72 10 in your contacts as "CallMeBot"
# 2. Send this exact message to that number on WhatsApp:
#    I allow callmebot to send me messages
# 3. You'll receive your API key back via WhatsApp within seconds
# 4. Add to Railway env vars:
#    CALLMEBOT_API_KEY = <key you received>
#    WHATSAPP_PHONE = 254701475000  (no + sign)
# ============================================

def send_whatsapp_notification(message):
    """Send WhatsApp message to Eli via CallMeBot free API."""
    api_key = os.getenv('CALLMEBOT_API_KEY')
    phone = os.getenv('WHATSAPP_PHONE', '254701475000')

    if not api_key:
        print("WARNING: CALLMEBOT_API_KEY not set — WhatsApp notification skipped")
        return False

    try:
        encoded_message = quote(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded_message}&apikey={api_key}"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            print(f"✅ WhatsApp sent to +{phone} via CallMeBot")
            return True
        else:
            print(f"❌ CallMeBot error: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        print(f"❌ WhatsApp send failed: {e}")
        return False


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
# TOOL COMPLETION EMAIL — ISSUE 1 FIX
# NEW: Send user their results + CTA after Tool #3/4/5
# ============================================

def send_tool_completion_email(user_email, tool_number, result_data):
    """
    Send user their audit results via email immediately after
    completing Tool #3, #4, or #5 and submitting their email.
    Previously: user got nothing after tool completion.
    Now: they get results summary + deep-link back to Nuru.
    """
    resend_api_key = os.getenv('RESEND_API_KEY')
    from_email = os.getenv('FROM_EMAIL', 'nuru@eliombogo.com')

    if not resend_api_key:
        print("WARNING: RESEND_API_KEY not set — tool completion email skipped")
        return False

    if not user_email:
        print("WARNING: No user email — tool completion email skipped")
        return False

    # Build tool-specific subject, headline, and summary
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
        summary_lines = [
            f"Readiness Score: {result_data.get('readiness_score', 0)}/100",
        ]
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

    # Build Nuru deep-link with session context
    nuru_url = "https://eliombogo.com/#nuru"
    if result_data.get('session_id'):
        nuru_url = f"https://eliombogo.com/#nuru?session={result_data['session_id']}"

    summary_html = ''.join([
        f"<p style='margin: 6px 0;'>✅ <strong>{line}</strong></p>"
        for line in summary_lines
    ])
    summary_text = '\n'.join([f"✅ {line}" for line in summary_lines])

    html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
  <div style="background: #1a2332; color: white; padding: 24px; border-radius: 8px 8px 0 0;">
    <h2 style="margin: 0; color: #10b981;">LocalOS Intelligence Platform</h2>
    <p style="margin: 8px 0 0; color: #9ca3af; font-size: 14px;">{headline}</p>
  </div>
  <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb;">
    <h3 style="color: #1a2332; margin-top: 0;">Your Results</h3>
    {summary_html}
  </div>
  <div style="background: white; padding: 24px; border: 1px solid #e5e7eb; border-top: none;">
    <p style="color: #374151; margin-top: 0;">
      Nuru has analysed your results and can show you exactly what to fix first,
      how long it takes, and what it costs — based on your specific numbers.
    </p>
    <div style="text-align: center; margin: 24px 0;">
      <a href="{nuru_url}"
         style="background: #10b981; color: white; padding: 14px 28px; border-radius: 6px;
                text-decoration: none; font-weight: bold; font-size: 16px; display: inline-block;">
        {cta_text}
      </a>
    </div>
    <p style="color: #6b7280; font-size: 13px; text-align: center;">
      Or book a discovery call:
      <a href="https://calendly.com/eli-eliombogo/discovery-call" style="color: #10b981;">
        calendly.com/eli-eliombogo/discovery-call
      </a>
    </p>
  </div>
  <div style="background: #1a2332; padding: 16px; border-radius: 0 0 8px 8px; text-align: center;">
    <p style="color: #9ca3af; font-size: 12px; margin: 0;">
      LocalOS — Intelligence Waste Auditors | eliombogo.com
    </p>
  </div>
</div>"""

    text = f"""{headline}

{summary_text}

Nuru has your results and can map out exactly what to fix first.

Talk to Nuru: {nuru_url}
Book a call: https://calendly.com/eli-eliombogo/discovery-call

LocalOS — eliombogo.com
"""

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': from_email,
                'to': [user_email],
                'subject': subject,
                'html': html,
                'text': text
            },
            timeout=10
        )

        if response.status_code in [200, 201]:
            print(f"✅ Tool #{tool_number} completion email sent to {user_email}")
            return True
        else:
            print(f"❌ Tool completion email failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        print(f"❌ Tool completion email error: {e}")
        return False


# ============================================
# TOOL COMPLETION ENDPOINT — ISSUE 1 FIX
# NEW: Tools #3/4/5 call this after user submits email
# ============================================

@app.route('/api/notify-completion', methods=['POST'])
def notify_completion():
    """
    Called by Tools #3, #4, #5 when user enters email after completing audit.
    Sends user their results + CTA to talk to Nuru.
    Also sends Eli a WhatsApp alert (warm lead signal).

    Expected payload:
    {
        "tool_number": 3,
        "user_email": "user@example.com",
        "session_id": "abc123",
        "result_data": { ...tool-specific fields... }
    }
    """
    try:
        data = request.json
        tool_number = data.get('tool_number')
        user_email = data.get('user_email')
        session_id = data.get('session_id')
        result_data = data.get('result_data', {})

        if not tool_number or not user_email:
            return jsonify({'error': 'tool_number and user_email are required'}), 400

        # Attach session_id so email deep-links back to Nuru with context
        result_data['session_id'] = session_id

        # Send user their results
        email_sent = send_tool_completion_email(user_email, tool_number, result_data)

        # Alert Eli on WhatsApp — warm lead (they completed a tool AND gave email)
        whatsapp_message = (
            f"🔔 Tool #{tool_number} completed\n"
            f"Email captured: {user_email}\n"
            f"Session: {session_id}\n"
            f"LocalOS — eliombogo.com"
        )
        send_whatsapp_notification(whatsapp_message)

        return jsonify({
            'success': email_sent,
            'message': f'Tool #{tool_number} completion email {"sent" if email_sent else "failed"} to {user_email}'
        })

    except Exception as e:
        print(f"❌ notify_completion error: {e}")
        return jsonify({'error': str(e)}), 500


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
    
    user_text = ' '.join([m['content'] for m in messages if m['role'] == 'user'])
    all_text = ' '.join([m['content'] for m in messages])
    
    lead_data = {}

    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_text)
    if email_match:
        lead_data['email'] = email_match.group(0)

    phone_match = re.search(r'(\+?\d[\d\s\-().]{7,}\d)', user_text)
    if phone_match:
        lead_data['phone'] = phone_match.group(0).strip()

    budget_match = re.search(r'\$[\d,]+[kK]?|\b(\d+[kK])\s*(budget|dollars?|USD)|\bbudget\s*(of|is|around|about)?\s*\$?[\d,]+[kK]?', user_text, re.IGNORECASE)
    if budget_match:
        lead_data['budget'] = budget_match.group(0).strip()
    
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
    if user_messages:
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
# NOTIFY ELI — QUALIFIED LEAD (EMAIL + WHATSAPP)
# UPDATED: now fires WhatsApp alongside existing email
# ============================================

def notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts):
    """Notify Eli via email AND WhatsApp when qualified lead detected"""
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

        # Send email (existing)
        email_sent = send_email_notification(
            subject=f"🎯 Qualified Lead - LocalOS | Conversation {conversation_id}",
            body_text=body,
            body_html=html
        )

        # NEW: Send WhatsApp alert (short, actionable)
        whatsapp_msg = (
            f"🎯 QUALIFIED LEAD\n"
            f"Company: {lead_data.get('company', 'Unknown')}\n"
            f"Budget: {lead_data.get('budget', 'Not stated')}\n"
            f"Contact: {lead_data.get('email', 'Not captured')}\n"
            f"Problem: {str(lead_data.get('problem', ''))[:100]}\n"
            f"Check email for full details."
        )
        whatsapp_sent = send_whatsapp_notification(whatsapp_msg)

        # Post to Google Sheets webhook (existing)
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

        print(f"Eli notified (conversation {conversation_id}) — Email: {email_sent}, WhatsApp: {whatsapp_sent}, Webhook: {webhook_sent}")

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
            lead_data = extract_lead_data_from_history(conversation_id, cursor)

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
# TEST ENDPOINTS
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


@app.route('/api/test-whatsapp', methods=['GET'])
def test_whatsapp():
    """
    NEW: Test WhatsApp via CallMeBot.
    Hit this URL after setting CALLMEBOT_API_KEY in Railway vars.
    URL: tool6-client-intake-production.up.railway.app/api/test-whatsapp
    """
    sent = send_whatsapp_notification(
        f"🧪 CallMeBot test — LocalOS Nuru backend\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"If you see this, WhatsApp notifications are working ✅"
    )
    return jsonify({
        'success': sent,
        'message': 'WhatsApp test sent ✅' if sent else 'WhatsApp test failed ❌ — check CALLMEBOT_API_KEY in Railway vars',
        'setup_reminder': (
            'Save +34 644 59 72 10 as CallMeBot. '
            'Send: I allow callmebot to send me messages. '
            'Add the API key you receive to Railway as CALLMEBOT_API_KEY.'
        )
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)