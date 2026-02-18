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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()
from utils.model_router import get_model

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
# EMAIL NOTIFICATION VIA GMAIL SMTP
# ============================================

def send_email_notification(subject, body_text, body_html=None):
    """Send email via Gmail SMTP (replaced SendGrid)"""
    gmail_user = os.getenv('GMAIL_USER')  # elytsend@gmail.com
    gmail_password = os.getenv('GMAIL_APP_PASSWORD')  # 16-char app password
    notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
    
    if not gmail_user or not gmail_password:
        print("WARNING: Gmail credentials not set - email notification skipped")
        return False
    
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = gmail_user
        msg['To'] = notify_email
        
        # Attach text and HTML parts
        text_part = MIMEText(body_text, 'plain')
        msg.attach(text_part)
        
        if body_html:
            html_part = MIMEText(body_html, 'html')
            msg.attach(html_part)
        
        # Send via Gmail SMTP using SSL port 465 (Railway blocks port 587)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.send_message(msg)
        
        print(f"✅ Email sent to {notify_email} via Gmail SMTP")
        return True
            
    except Exception as e:
        print(f"❌ Email send failed: {e}")
        return False


# ============================================
# NOTIFY ELI - QUALIFIED LEAD
# ============================================

def notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts):
    """Notify Eli via email when qualified lead detected"""
    try:
        # Build plain text body
        body = f"""QUALIFIED LEAD - LocalOS
{'='*50}

LEAD DETAILS:
Company: {lead_data.get('company', 'Not provided')}
Industry: {lead_data.get('industry', 'Not provided')}
Contact: {lead_data.get('email', 'Not provided')}

QUALIFICATION:
Budget: {lead_data.get('budget', 'Stated in conversation')}
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

        # Build HTML body
        html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
  <div style="background: #1a2332; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
    <h2 style="margin: 0; color: #10b981;">🎯 Qualified Lead - LocalOS</h2>
    <p style="margin: 5px 0 0; color: #9ca3af; font-size: 14px;">{datetime.now().strftime('%B %d, %Y at %H:%M UTC')}</p>
  </div>
  
  <div style="background: #f9fafb; padding: 20px; border: 1px solid #e5e7eb;">
    <h3 style="color: #1a2332; border-bottom: 2px solid #10b981; padding-bottom: 8px;">Lead Details</h3>
    <p><strong>Company:</strong> {lead_data.get('company', 'Not provided')}</p>
    <p><strong>Industry:</strong> {lead_data.get('industry', 'Not provided')}</p>
    <p><strong>Contact:</strong> {lead_data.get('email', 'Not provided')}</p>
    <p><strong>Budget:</strong> {lead_data.get('budget', 'Stated in conversation')}</p>
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

        # Send email via Gmail SMTP
        email_sent = send_email_notification(
            subject=f"🎯 Qualified Lead - LocalOS | Conversation {conversation_id}",
            body_text=body,
            body_html=html
        )

        # Also post to Google Sheets (keep existing webhook as backup)
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
        
        # SAFE: Only creates if not exists - never drops data
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
# ============================================

def check_qualification(conversation_id, assistant_message, user_message, audit_contexts, cursor):
    qualified = False
    lead_data = {}
    
    msg_lower = assistant_message.lower()
    user_lower = (user_message or '').lower()
    combined = msg_lower + ' ' + user_lower

    if '$' in combined or 'budget' in combined:
        qualified = True
        lead_data['budget'] = 'Stated in conversation'

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
            cursor.execute(
                """INSERT INTO leads (conversation_id, qualification_status, notified_at)
                   VALUES (%s, %s, NOW())
                   RETURNING id""",
                (conversation_id, 'qualified')
            )
            
            notify_eli_qualified_lead(conversation_id, lead_data, audit_contexts)


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
    """Test endpoint to verify Gmail SMTP works - hit this URL to send test email"""
    try:
        print("🧪 TEST EMAIL - Attempting to send...")
        
        gmail_user = os.getenv('GMAIL_USER')
        gmail_password = os.getenv('GMAIL_APP_PASSWORD')
        notify_email = os.getenv('NOTIFY_EMAIL', 'eli@eliombogo.com')
        
        print(f"Gmail User: {gmail_user}")
        print(f"Notify Email: {notify_email}")
        print(f"Gmail Password Set: {'Yes' if gmail_password else 'No'}")
        
        if not gmail_user or not gmail_password:
            return jsonify({
                'success': False,
                'error': 'Gmail credentials not configured',
                'gmail_user': gmail_user,
                'has_password': bool(gmail_password)
            }), 500
        
        # Create test message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '🧪 TEST EMAIL - LocalOS Nuru'
        msg['From'] = gmail_user
        msg['To'] = notify_email
        
        body = f"""
This is a test email from Nuru backend.

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
Backend: Railway tool6-client-intake-production
Gmail User: {gmail_user}
Recipient: {notify_email}

If you receive this, Gmail SMTP is working correctly.
"""
        
        text_part = MIMEText(body, 'plain')
        msg.attach(text_part)
        
        print("📧 Connecting to Gmail SMTP (port 465 SSL)...")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            print("🔑 Logging in...")
            server.login(gmail_user, gmail_password)
            
            print("📤 Sending message...")
            server.send_message(msg)
        
        print(f"✅ TEST EMAIL SENT to {notify_email}")
        
        return jsonify({
            'success': True,
            'message': f'Test email sent to {notify_email}',
            'gmail_user': gmail_user,
            'timestamp': datetime.now().isoformat()
        }), 200
        
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ Gmail Authentication Failed: {e}")
        return jsonify({
            'success': False,
            'error': 'Gmail authentication failed',
            'details': str(e),
            'hint': 'Check GMAIL_APP_PASSWORD is correct 16-char app password'
        }), 401
        
    except Exception as e:
        print(f"❌ Email Test Failed: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)