// Nuru Chat Widget - LocalOS Client Intake Intelligence
const BACKEND_URL = 'https://tool6-client-intake-production.up.railway.app';
let sessionId = null;
let isWaitingForResponse = false;

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
    bubble.textContent = content;
    
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

// Send message to backend
async function sendNuruMessage() {
    if (isWaitingForResponse) return;
    
    const input = document.getElementById('nuruInput');
    const message = input.value.trim();
    
    if (!message) return;
    
    // Add user message
    addMessage(message, true);
    input.value = '';
    
    // Show typing
    isWaitingForResponse = true;
    showTyping();
    
    try {
        const response = await fetch(`${BACKEND_URL}/api/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message: message,
                session_id: getSessionId()
            })
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
document.addEventListener('DOMContentLoaded', function() {
    console.log('Nuru Chat Widget initialized');
    getSessionId(); // Generate session on load
});