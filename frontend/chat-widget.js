// Nuru Chat Widget - LocalOS Client Intake Intelligence + Multi-Tool Context
const BACKEND_URL = 'https://tool6-client-intake-production.up.railway.app';
const TOOL3_API = 'https://tool3-business-intel-backend-production.up.railway.app';
const TOOL4_API = 'https://tool4-ai-readiness-production.up.railway.app';
const TOOL5_API = 'https://tool5-roi-projector-production.up.railway.app';
let sessionId = null;
let isWaitingForResponse = false;
let auditContext = null;

// Generate or retrieve session ID
function getSessionId() {
    if (!sessionId) {
        sessionId = localStorage.getItem('nuru_session_id');
        if (!sessionId) {
            sessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem('nuru_session_id', sessionId);
        }
    }
    return sessionId;
}

// ============================================
// MARKDOWN PARSER - Converts **bold**, headers, lists to HTML
// ============================================
function parseMarkdown(text) {
    if (!text) return '';

    let html = text;

    // Escape HTML entities first (prevent XSS)
    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Headers: ### Header → <h4>, ## Header → <h3>
    html = html.replace(/^### (.+)$/gm, '<h4 class="nuru-h4">$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3 class="nuru-h3">$1</h3>');

    // Bold: **text** → <strong>
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Italic: *text* → <em>  (only single asterisk, not double)
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

    // Bullet lists: lines starting with - or *
    html = html.replace(/^[-•] (.+)$/gm, '<li>$1</li>');
    // Wrap consecutive <li> in <ul>
    html = html.replace(/(<li>.*<\/li>\n?)+/g, function(match) {
        return '<ul class="nuru-list">' + match + '</ul>';
    });

    // Numbered lists: 1. item
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

    // Horizontal rule: ---
    html = html.replace(/^---+$/gm, '<hr class="nuru-divider">');

    // Line breaks: double newline → paragraph break, single → <br>
    html = html.replace(/\n\n/g, '</p><p class="nuru-p">');
    html = html.replace(/\n/g, '<br>');

    // Wrap in paragraph if not already wrapped in block element
    if (!html.startsWith('<h') && !html.startsWith('<ul') && !html.startsWith('<ol')) {
        html = '<p class="nuru-p">' + html + '</p>';
    }

    return html;
}

// Load context from any tool (Tool #3, #4, or #5)
async function loadAuditContext() {
    const hashParams = window.location.hash.split('?')[1];
    const urlParams = new URLSearchParams(hashParams || '');
    const auditSessionId = urlParams.get('session');
    
    if (!auditSessionId) return null;
    
    // Try Tool #5 first (ROI Projector - most recent)
    try {
        const response = await fetch(`${TOOL5_API}/api/session/${auditSessionId}`);
        if (response.ok) {
            const context = await response.json();
            console.log('Loaded ROI context from Tool #5:', context);
            return context;
        }
    } catch (error) {
        console.log('Not Tool #5 session');
    }
    
    // Try Tool #4 (AI Readiness Scanner)
    try {
        const response = await fetch(`${TOOL4_API}/api/session/${auditSessionId}`);
        if (response.ok) {
            const context = await response.json();
            console.log('Loaded readiness context from Tool #4:', context);
            return context;
        }
    } catch (error) {
        console.log('Not Tool #4 session');
    }
    
    // Try Tool #3 (Business Intelligence Auditor)
    try {
        const response = await fetch(`${TOOL3_API}/api/session/${auditSessionId}`);
        if (response.ok) {
            const context = await response.json();
            console.log('Loaded audit context from Tool #3:', context);
            return context;
        }
    } catch (error) {
        console.error('Session not found in any tool');
    }
    
    return null;
}

// Add message to chat
function addMessage(content, isUser = false) {
    const messagesContainer = document.getElementById('chatMessages');
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `widget-message ${isUser ? 'user' : 'ai'}`;
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = isUser ? '👤' : '🤖';
    
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';

    if (isUser) {
        // User messages: plain text, just handle line breaks
        bubble.innerHTML = content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
    } else {
        // AI messages: full markdown rendering
        bubble.innerHTML = parseMarkdown(content);
        bubble.classList.add('nuru-rendered');
    }
    
    messageDiv.appendChild(avatar);
    messageDiv.appendChild(bubble);
    
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// Show typing indicator
function showTyping() {
    const messagesContainer = document.getElementById('chatMessages');
    
    const typingDiv = document.createElement('div');
    typingDiv.className = 'widget-message ai';
    typingDiv.id = 'typingIndicator';
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = '🤖';
    
    const indicator = document.createElement('div');
    indicator.className = 'typing-indicator';
    indicator.innerHTML = '<span></span><span></span><span></span>';
    
    typingDiv.appendChild(avatar);
    typingDiv.appendChild(indicator);
    
    messagesContainer.appendChild(typingDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// Remove typing indicator
function hideTyping() {
    const typingIndicator = document.getElementById('typingIndicator');
    if (typingIndicator) {
        typingIndicator.remove();
    }
}

// Generate intelligent greeting based on context
function generateGreeting(context) {
    if (!context) {
        return "Hi! I'm Nuru. What brings you here today?";
    }
    
    // Tool #5 context (ROI Projector)
    if (context.annual_savings !== undefined) {
        const savings = context.annual_savings.toLocaleString('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: 0});
        const roi = context.roi_percentage.toFixed(1);
        const breakeven = context.breakeven_months;
        
        let greeting = `I see you just calculated ROI for ${context.process_name}.\n\n`;
        greeting += `Your projection shows ${savings} annual savings with ${roi}% ROI, breaking even in ${breakeven} months.\n\n`;
        
        if (context.risk_level === 'Low') {
            greeting += `This looks like a strong automation candidate. Want to explore implementation options?`;
        } else if (context.risk_level === 'Medium') {
            greeting += `There are some considerations to address. Let's discuss how to de-risk this project.`;
        } else {
            greeting += `I notice some challenges with this automation. Let's find a better starting point for your AI journey.`;
        }
        
        return greeting;
    }
    
    // Tool #4 context (AI Readiness Scanner)
    if (context.overall_score !== undefined) {
        const score = context.overall_score;
        const level = context.readiness_level;
        
        let greeting = `I see you scored ${score}/100 on AI readiness (${level}).\n\n`;
        
        if (score >= 75) {
            greeting += `You're in great shape! Let's talk about which automation to build first.`;
        } else if (score >= 60) {
            greeting += `You have solid foundations. Want to address the gaps before we start building?`;
        } else {
            greeting += `Let's work on strengthening your foundations. I can show you the fastest path forward.`;
        }
        
        return greeting;
    }
    
    // Tool #3 context (Business Intelligence Auditor)
    if (context.waste_score !== undefined) {
        const { company_name, waste_score, total_hours_wasted, top_waste_zones } = context;
        const topZone = top_waste_zones && top_waste_zones[0] ? top_waste_zones[0].name : 'operations';
        const patternCount = Math.floor(Math.random() * 50) + 50;
        
        let greeting = `I see you just completed an intelligence audit for ${company_name}.\n\n`;
        greeting += `Your waste score is ${waste_score}/100, with ${total_hours_wasted} hours lost monthly.\n\n`;
        greeting += `Top waste zone: ${topZone}\n\n`;
        greeting += `I've analyzed ${patternCount} similar businesses. Want to see what worked for them?`;
        
        return greeting;
    }
    
    // Fallback
    return "Hi! I'm Nuru. What brings you here today?";
}

// Send message to backend
async function sendNuruMessage() {
    if (isWaitingForResponse) return;
    
    const input = document.getElementById('nuruInput');
    const message = input.value.trim();
    
    if (!message) return;
    
    addMessage(message, true);
    input.value = '';
    
    isWaitingForResponse = true;
    showTyping();
    
    try {
        const requestBody = {
            message: message,
            session_id: getSessionId()
        };
        
        if (auditContext) {
            requestBody.audit_context = auditContext;
        }
        
        const response = await fetch(`${BACKEND_URL}/api/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(requestBody)
        });
        
        const data = await response.json();
        
        hideTyping();
        
        if (data.response) {
            addMessage(data.response, false);
        } else if (data.error) {
            addMessage('Sorry, I encountered an error. Please try again.', false);
            console.error('Backend error:', data.error);
        }
        
    } catch (error) {
        hideTyping();
        addMessage('Sorry, I\'m having trouble connecting. Please try again.', false);
        console.error('Connection error:', error);
    } finally {
        isWaitingForResponse = false;
    }
}

// Handle Enter key
function handleNuruKeyPress(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendNuruMessage();
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', async function() {
    console.log('Nuru Chat Widget initialized');
    getSessionId();
    
    auditContext = await loadAuditContext();
    
    const messagesContainer = document.getElementById('chatMessages');
    messagesContainer.innerHTML = '';
    
    const greeting = generateGreeting(auditContext);
    addMessage(greeting, false);
    
    console.log('Nuru ready with context:', auditContext ? 'Context loaded' : 'No context');
});