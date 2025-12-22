-- Conversations table (stores all chat messages)
CREATE TABLE conversations (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'active',
    lead_quality_score INTEGER DEFAULT 0
);

-- Messages table (individual messages in conversations)
CREATE TABLE messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Leads table (qualified leads extracted from conversations)
CREATE TABLE leads (
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
    qualification_status VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Context data table (detected context from conversations)
CREATE TABLE context_data (
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