-- ========================================================
-- White-Label CRM Starter Settings for MySQL 8
-- Compatible with Coolify MySQL database
-- ========================================================

INSERT INTO settings (`key`, value, updated_at)
VALUES
(
  'KNOWLEDGE_BASE_JSON',
  '{"raw_markdown":"# DIGITAL MARKETING & AI AUTOMATION AGENCY\\n\\nBusiness Name:\\nGrowth & AI Solutions\\n\\nServices Offered:\\n1. Facebook & Instagram Ads Management (Meta Ads Manager)\\n2. Instagram & Social Media Management\\n3. AI Voice Calling (Inbound/Outbound Agents)\\n4. AI WhatsApp CRM & Automation\\n5. Interior ERP & Industry Solutions\\n6. AI Social Media Post & Creative Automation\\n7. Lead Generation & Conversion Funnels","company_profile":{"business_name":"Growth & AI Agency","legal_name":"Growth & AI Solutions Ltd.","short_description":"Full-funnel digital marketing, AI voice calling, and WhatsApp automation agency.","about_us":"We are a premier digital marketing and AI automation agency helping businesses scale through Meta Ads, content management, AI voice agents, and WhatsApp CRM systems.","industry_type":"Digital Marketing & AI Agency","owner_name":"","website":"","email":"growth@example.com","phone":"","whatsapp_number":""},"contact_details":{"primary_phone":"","whatsapp_support_number":"","general_email":"growth@example.com","physical_address":"","google_maps_link":""},"services":[{"name":"Facebook & Instagram Ads Management","description":"Full-funnel Meta Ads Manager campaigns, audience targeting, ROAS optimization, and retargeting."},{"name":"Instagram & Social Media Management","description":"End-to-end content calendar, creative reels, captions, engagement, and brand growth."},{"name":"AI Voice Calling","description":"Conversational AI voice agents for inbound lead qualification and outbound call campaigns."},{"name":"AI WhatsApp CRM & Automation","description":"Automated WhatsApp chatbot, drip follow-ups, broadcast campaigns, and CRM sync."},{"name":"Interior ERP & Industry Solutions","description":"Tailored ERP workflow management, client portal, estimates, and project milestone tracking."},{"name":"AI Social Media Automation","description":"Automated creative generation, scheduled posting, and AI-assisted copywriting workflows."},{"name":"Lead Generation & Conversion Funnels","description":"High-converting landing pages, paid traffic funnels, and automated CRM pipeline routing."}],"faq":[{"question":"What services do you provide?","answer":"We provide Facebook & Instagram Ads Management, Instagram & Social Media Management, AI Voice Calling Agents, AI WhatsApp CRM & Automation, Interior ERP Solutions, and AI Social Media Automation."},{"question":"How do your AI Voice and WhatsApp CRM services work?","answer":"We configure 24/7 intelligent voice and WhatsApp bots that instantly capture, qualify, and book leads directly into your CRM with automated follow-ups."},{"question":"How can I schedule a discovery call or consultation?","answer":"Simply reply here on WhatsApp or call our team with your business name and requirements, and we will schedule an audit and discovery call."}],"lead_qualification":{"qualification_criteria":["Business / Brand name","Service required (Ads, SMM, Voice AI, WhatsApp CRM, ERP, Automation)","Target audience or monthly ad budget","Preferred start timeline"]},"whatsapp_ai_instructions":{"tone":"Professional, energetic, results-driven, and consultative.","booking_instructions":"Identify the client\\''s primary business goal or service requirement (e.g. Meta Ads, AI Voice, WhatsApp CRM), then propose a discovery call."}}',
  NOW(6)
),
(
  'SYSTEM_PROMPT',
  'You are an AI assistant representing our team. Use KNOWLEDGE_BASE_JSON as the single source of truth. Answer questions about services, pricing, policies, location, working hours, and appointments strictly based on the knowledge base. If information is not in the knowledge base, state politely that the team will verify and follow up.',
  NOW(6)
),
(
  'WHATSAPP_AI_SYSTEM_PROMPT',
  'You are a friendly and professional WhatsApp chat assistant. Use KNOWLEDGE_BASE_JSON as the single source of truth. Keep answers concise, easy to read on mobile, and prompt the user towards scheduling an appointment or sharing their requirements.',
  NOW(6)
),
(
  'OUTBOUND_CALLING_ENABLED',
  'false',
  NOW(6)
),
(
  'ENABLE_AUTOMATED_OUTBOUND',
  'false',
  NOW(6)
),
(
  'OUTBOUND_FIXED_GREETING_ENABLED',
  'true',
  NOW(6)
),
(
  'OUTBOUND_GREETING_MODE',
  'fixed',
  NOW(6)
),
(
  'OUTBOUND_FIXED_GREETING',
  'Hi, this is our customer support team. Is this a good time to speak for a minute?',
  NOW(6)
),
(
  'FOLLOWUP_ENABLED',
  'true',
  NOW(6)
),
(
  'FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS',
  '24',
  NOW(6)
),
(
  'FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS',
  '3',
  NOW(6)
)
ON DUPLICATE KEY UPDATE
  value = VALUES(value),
  updated_at = NOW(6);

-- Ensure default lead status exists
INSERT INTO lead_statuses (name, color)
VALUES ('New Lead', '#64748b')
ON DUPLICATE KEY UPDATE
  color = VALUES(color);
