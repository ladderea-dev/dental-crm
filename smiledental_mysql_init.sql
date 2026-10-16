-- ==============================================================================
-- SMILE DENTAL AND IMPLANT CENTRE - MYSQL DATABASE INITIALIZATION SCRIPT
-- ==============================================================================
-- Target: Coolify MySQL Database for Smile Dental & Implant Centre
-- Clinic: Smile Dental and Implant Centre (Kapra, ECIL, AS Rao Nagar, Secunderabad)
-- Website: https://smiledentalandimplantcentre.com/
-- Map CID: 11928096181775791226 (Near Kapra Circle, Saket Road, ECIL)
-- Doctors: Dr. P. Sudhakar (BDS, MDS) & Dr. K. Mounika (BDS, PGDCC)
-- ==============================================================================

-- 1. Ensure Dental Pipeline Statuses Exist
INSERT INTO lead_statuses (id, name, color, created_at)
VALUES
  (UUID(), 'New Patient Enquiry', '#64748b', NOW(6)),
  (UUID(), 'Symptoms Evaluated', '#8b5cf6', NOW(6)),
  (UUID(), 'Consultation Scheduled', '#2563eb', NOW(6)),
  (UUID(), 'Token / Slot Confirmed', '#0284c7', NOW(6)),
  (UUID(), 'Doctor Consultation Done', '#10b981', NOW(6)),
  (UUID(), 'Treatment Plan In Progress', '#f59e0b', NOW(6)),
  (UUID(), 'Treatment Completed', '#059669', NOW(6)),
  (UUID(), 'Follow-up Due', '#f97316', NOW(6)),
  (UUID(), 'No Show / Cancelled', '#dc2626', NOW(6))
ON DUPLICATE KEY UPDATE
  color = VALUES(color);

-- 2. Pre-Seed Appointment Staff (Specialist Doctors)
INSERT INTO appointment_staff (
  id, name, email, whatsapp_number, role,
  working_days, start_time, end_time,
  active, round_robin_order, notes,
  created_at, updated_at
)
VALUES
(
  'stf-doc-sudhakar',
  'Dr. P. Sudhakar (BDS, MDS - Implantologist & Periodontist)',
  'smiledentalandimplantcentre@gmail.com',
  '+917396529265',
  'Senior Consultant & Chief Implantologist',
  '["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]',
  '09:00:00',
  '21:00:00',
  TRUE,
  1,
  'Senior Periodontist & Implantologist with 15+ years experience. Specializes in Dental Implants, All-on-4, Full Mouth Rehabilitation, Painless Root Canals, and Laser Gum Surgeries.',
  NOW(6),
  NOW(6)
),
(
  'stf-doc-mounika',
  'Dr. K. Mounika (BDS, PGDCC - Cosmetic Dental Surgeon)',
  'smiledentalandimplantcentre@gmail.com',
  '+919490618635',
  'Cosmetic Dentist & Facial Aesthetics Specialist',
  '["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]',
  '09:00:00',
  '20:00:00',
  TRUE,
  2,
  'Consultant Cosmetic Dentist & Facial Aesthetics Specialist. Specializes in Smile Designing, Porcelain Veneers, Teeth Whitening, Clear Aligners, and Pediatric Dentistry.',
  NOW(6),
  NOW(6)
)
ON DUPLICATE KEY UPDATE
  name = VALUES(name),
  role = VALUES(role),
  whatsapp_number = VALUES(whatsapp_number),
  start_time = VALUES(start_time),
  end_time = VALUES(end_time),
  notes = VALUES(notes),
  active = VALUES(active),
  updated_at = NOW(6);

-- 3. Pre-Seed Settings & Dental Knowledge Base
INSERT INTO settings (`key`, value, updated_at)
VALUES
(
  'BUSINESS_NAME',
  'Smile Dental and Implant Centre',
  NOW(6)
),
(
  'BUSINESS_TYPE',
  'Dental Clinic & Implant Centre',
  NOW(6)
),
(
  'DEFAULT_SERVICE_TYPE',
  'Dental Checkup & Consultation',
  NOW(6)
),
(
  'BUSINESS_CITY',
  'Secunderabad / Hyderabad',
  NOW(6)
),
(
  'APP_COUNTRY',
  'India',
  NOW(6)
),
(
  'APP_COUNTRY_CODE',
  '+91',
  NOW(6)
),
(
  'APP_TIMEZONE',
  'Asia/Kolkata',
  NOW(6)
),
(
  'APPOINTMENT_TIMEZONE',
  'Asia/Kolkata',
  NOW(6)
),
(
  'WHATSAPP_DISPLAY_TIMEZONE',
  'Asia/Kolkata',
  NOW(6)
),
(
  'APPOINTMENT_APPOINTMENT_NAME',
  'Dental Consultation / Checkup',
  NOW(6)
),
(
  'APPOINTMENT_DEFAULT_SERVICE_TYPE',
  'Dental Checkup & Consultation',
  NOW(6)
),
(
  'APPOINTMENT_DELIVERY_MODE',
  'in_clinic',
  NOW(6)
),
(
  'APPOINTMENT_LOCATION_LABEL',
  'Clinic Address (Kapra, ECIL)',
  NOW(6)
),
(
  'APPOINTMENT_STAFF_LABEL',
  'Doctor / Specialist',
  NOW(6)
),
(
  'APPOINTMENT_DEFAULT_STAFF_ROLE',
  'dental_specialist',
  NOW(6)
),
(
  'APPOINTMENT_INTAKE_PROMPT',
  'Before I confirm your dental visit, please let me know the patient name, age, primary dental concern (e.g. Toothache, Dental Implants, Root Canal, Braces/Aligners, Cleaning), and preferred doctor or time slot.',
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
  'Hello! This is Smile Dental and Implant Centre in Kapra, ECIL. We received your appointment enquiry. Is this a good time to speak?',
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
),
(
  'SYSTEM_PROMPT',
  'You are the AI Patient Care Assistant for Smile Dental and Implant Centre, located in Kapra / ECIL, Secunderabad. Answer patient questions warmly, accurately, and reassuringly strictly based on KNOWLEDGE_BASE_JSON. Emphasize painless dental treatments, advanced hygiene/sterilization, and expert care by Dr. P. Sudhakar and Dr. K. Mounika. Encourage booking an in-clinic consultation for clinical assessment and digital X-rays.',
  NOW(6)
),
(
  'WHATSAPP_AI_SYSTEM_PROMPT',
  'You are the friendly and professional WhatsApp Patient Care Assistant for Smile Dental and Implant Centre, ECIL / Kapra, Secunderabad. Use KNOWLEDGE_BASE_JSON as your single source of truth. Keep responses concise, warm, and formatted cleanly for mobile reading with bullet points. Address dental concerns empathetically (e.g. pain relief, dental implants, root canal, teeth alignment/braces, smile designing). Prompt patients to schedule their in-clinic consultation with preferred date, time, and patient name.',
  NOW(6)
),
(
  'AIRZEN_SYSTEM_USERS_JSON',
  '[{"id": "usr-admin-01", "username": "admin", "email": "smiledentalandimplantcentre@gmail.com", "full_name": "Clinic Admin / Managing Director", "role": "admin", "password_hash": "6289cf91545be423c06f469d36522459$805e8f8fed70af4538d7fa7375a5035df0004db2ab1bb01ac31bbaa3b86d504e", "active": true, "created_at": "2026-10-16T12:00:00Z"}, {"id": "usr-reception-01", "username": "reception", "email": "reception@smiledentalandimplantcentre.com", "full_name": "Smile Dental Reception Desk", "role": "telecaller", "password_hash": "bdfee847ca7602d59544c7229c0c3926$b98db325089f3db649408c0d328ba862f580e221ce97f229ba4321402a811ab2", "active": true, "created_at": "2026-10-16T12:00:00Z"}, {"id": "usr-doc-sudhakar", "username": "dr_sudhakar", "email": "dr.sudhakar@smiledentalandimplantcentre.com", "full_name": "Dr. P. Sudhakar (Chief Implantologist)", "role": "manager", "password_hash": "b6acbd3a1d47a3036af5cceb5afd0c81$88c79b70535145981bf10371b7a1fcff78286e6e72f423a0559285c535561e66", "active": true, "created_at": "2026-10-16T12:00:00Z"}, {"id": "usr-doc-mounika", "username": "dr_mounika", "email": "dr.mounika@smiledentalandimplantcentre.com", "full_name": "Dr. K. Mounika (Cosmetic Dental Specialist)", "role": "manager", "password_hash": "b6acbd3a1d47a3036af5cceb5afd0c81$88c79b70535145981bf10371b7a1fcff78286e6e72f423a0559285c535561e66", "active": true, "created_at": "2026-10-16T12:00:00Z"}]',
  NOW(6)
),
(
  'KNOWLEDGE_BASE_JSON',
  '{"raw_markdown": "# SMILE DENTAL AND IMPLANT CENTRE\\n\\n**Address:** H.No. 1-1-308/8, Plot No. 8, 1st Floor, Above K. Rathod Jewellers, Sri Ram Nagar Colony, Near Kapra Circle, Saket Road, ECIL, Secunderabad, Telangana 500062\\n**Phone:** +91 7396529265, +91 9490618635\\n**Email:** smiledentalandimplantcentre@gmail.com\\n**Website:** https://smiledentalandimplantcentre.com/\\n**Google Maps:** https://maps.google.com/?cid=11928096181775791226\\n\\n**Operating Hours:**\\n- Monday to Saturday: 9:00 AM - 9:00 PM\\n- Sunday: 10:00 AM - 1:00 PM (Prior Appointment & Emergencies)\\n\\n**Key Doctors:**\\n- Dr. P. Sudhakar (BDS, MDS) - Senior Periodontist & Implantologist (15+ Years Experience)\\n- Dr. K. Mounika (BDS, PGDCC) - Cosmetic Dentist & Facial Aesthetics Specialist\\n\\n**Key Services:**\\n1. Dental Implants & Full Mouth Rehabilitation\\n2. Painless Single-Sitting Root Canal Treatment (RCT)\\n3. Invisalign & Clear Aligners / Orthodontic Braces\\n4. Cosmetic Dentistry & Smile Makeover (Porcelain Veneers, 1-Hour Teeth Whitening)\\n5. Metal-Free Zirconia Crowns & Ceramic Bridges\\n6. Ultrasonic Teeth Cleaning, Scaling & Gum Polishing\\n7. Painless Wisdom Tooth Extraction & Oral Surgery\\n8. Pediatric Dentistry & Kids Dental Care\\n9. Full & Flexible Partial Dentures\\n10. Emergency Toothache Relief & Dental Trauma Care", "company_profile": {"business_name": "Smile Dental and Implant Centre", "legal_name": "Smile Dental and Implant Centre", "short_description": "Best Dental Clinic in Kapra, ECIL, AS Rao Nagar, Secunderabad. Painless Root Canal, Dental Implants, Invisalign & Smile Designing.", "about_us": "Smile Dental and Implant Centre is widely recognized as one of the best dental clinics in Secunderabad and Hyderabad (near Kapra Circle, Saket Road, ECIL). Led by senior dental surgeon Dr. P. Sudhakar (MDS - Periodontist & Implantologist with 15+ years of experience) and Dr. K. Mounika (BDS, PGDCC - Cosmetic Dental Specialist), our clinic features state-of-the-art sterilization, digital RVG X-rays, rotary painless root canals, top-tier dental implants, invisible aligners, and comprehensive family dental care.", "industry_type": "Dental Clinic & Implant Centre", "owner_name": "Dr. P. Sudhakar (BDS, MDS)", "website": "https://smiledentalandimplantcentre.com/", "email": "smiledentalandimplantcentre@gmail.com", "phone": "+917396529265", "whatsapp_number": "+917396529265"}, "contact_details": {"primary_phone": "+91 7396529265", "support_phone": "+91 9490618635", "whatsapp_support_number": "+91 7396529265", "general_email": "smiledentalandimplantcentre@gmail.com", "physical_address": "H.No. 1-1-308/8, Plot No. 8, 1st Floor, Above K. Rathod Jewellers, Sri Ram Nagar Colony, Near Kapra Circle, Saket Road, ECIL, Secunderabad, Telangana 500062", "city": "Secunderabad / Hyderabad", "state": "Telangana", "country": "India", "google_maps_link": "https://maps.google.com/?cid=11928096181775791226"}, "working_hours": {"timezone": "Asia/Kolkata", "opening_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], "opening_time": "09:00", "closing_time": "21:00", "holiday_notes": "Open Monday to Saturday 9:00 AM to 9:00 PM. Sundays open 10:00 AM to 1:00 PM for prior appointments and emergency care.", "emergency_support_available": true}, "services": [{"name": "Dental Implants & Full Mouth Rehabilitation", "description": "Permanent, natural-looking tooth replacement using biocompatible titanium implants. Offers Single Tooth, Multiple Teeth, and All-on-4 / All-on-6 full arch replacements by senior implantologist Dr. P. Sudhakar."}, {"name": "Painless Single-Sitting Root Canal Treatment (RCT)", "description": "Painless treatment to save infected or badly decayed teeth using rotary endodontic equipment, digital apex locators, and sterile procedures in just 1-2 sittings."}, {"name": "Invisalign & Clear Aligners / Braces", "description": "Custom clear invisible aligners and orthodontic braces (ceramic, metal, and self-ligating) to straighten crooked teeth, close gaps, and correct bite alignment for teens and adults."}, {"name": "Cosmetic Dentistry & Smile Designing", "description": "Personalized smile makeovers including porcelain and composite veneers, 1-hour laser teeth whitening, gap closures, and gummy smile correction by Dr. K. Mounika."}, {"name": "Dental Crowns & Bridges", "description": "High-strength, natural-looking metal-free Zirconia crowns with 5 to 15-year warranty, CAD/CAM ceramic crowns, and aesthetic dental bridges to restore chewing function."}, {"name": "Teeth Cleaning, Scaling & Polishing", "description": "Advanced ultrasonic cleaning to remove stubborn plaque, tartar, coffee/tobacco stains, and cure bleeding gums (gingivitis), followed by protective fluoride polishing."}, {"name": "Wisdom Tooth Extraction & Oral Surgery", "description": "Gentle, painless extraction of impacted wisdom teeth and diseased teeth with minimal post-op discomfort and rapid healing."}, {"name": "Pediatric / Kids Dentistry", "description": "Child-friendly dental care including preventive fluoride coatings, cavity fillings, painless milk tooth root canal (pulpectomy), and habit-breaking appliances."}, {"name": "Dentures (Full & Partial)", "description": "Custom-fitted lightweight flexible dentures, BPS dentures, and implant-retained overdentures providing superior grip and natural chewing ability."}, {"name": "Emergency Toothache & Dental Trauma Care", "description": "Immediate relief for severe tooth pain, broken or cracked teeth, gum abscess, facial swellings, or dental trauma."}], "packages": [{"name": "Complete Dental Checkup & Digital X-Ray", "description": "Comprehensive oral examination, digital RVG X-ray, and personalized treatment plan discussion with senior specialist.", "fee_estimate": "₹300 - ₹500"}, {"name": "Ultrasonic Scaling & Polishing Package", "description": "Full mouth ultrasonic cleaning, stain removal, and protective fluoride polishing.", "fee_estimate": "₹1,000 - ₹1,500"}, {"name": "Single-Sitting RCT Package", "description": "Complete painless root canal procedure with advanced rotary tools and digital apex locators.", "fee_estimate": "Starting ₹3,500"}, {"name": "Dental Implant Consultation Package", "description": "3D bone evaluation, implant suitability assessment, and treatment planning by Dr. P. Sudhakar.", "fee_estimate": "₹500 consultation (Implants starting ₹20,000 - ₹35,000)"}], "faqs": [{"question": "Where is Smile Dental and Implant Centre located?", "answer": "We are located on the 1st Floor, Above K. Rathod Jewellers, Sri Ram Nagar Colony, Near Kapra Circle, Saket Road, ECIL, Secunderabad – 500062. Landmark: Right above K. Rathod Jewellers near Kapra Circle."}, {"question": "What are the clinic timings?", "answer": "Monday through Saturday from 9:00 AM to 9:00 PM. On Sundays, we are open from 10:00 AM to 1:00 PM by prior appointment."}, {"question": "Who are the chief doctors at Smile Dental?", "answer": "Dr. P. Sudhakar, BDS, MDS (Senior Consultant, Periodontist & Implantologist with over 15 years experience) and Dr. K. Mounika, BDS, PGDCC (Consultant Cosmetic Dentist & Facial Aesthetics Specialist)."}, {"question": "Is Root Canal Treatment (RCT) painful?", "answer": "No! At Smile Dental, we perform Root Canal Treatments using advanced local anesthesia, rotary endodontics, and digital apex locators, making the procedure virtually painless and comfortable."}, {"question": "How long does a Dental Implant procedure take?", "answer": "Placing a single dental implant usually takes just 20 to 30 minutes under local anesthesia. The implant integrates with the jawbone over 2 to 3 months, after which a permanent aesthetic crown (Zirconia/Ceramic) is placed."}, {"question": "Do I need to book an appointment before visiting?", "answer": "Prior appointment booking is highly recommended to ensure zero waiting time and prompt consultation with the specialist doctor. However, walk-in dental emergencies are always accommodated immediately."}, {"question": "Are clear aligners (Invisalign) available?", "answer": "Yes! We offer digital orthodontic scans and custom clear invisible aligners as well as ceramic/metal braces to align teeth without uncomfortable wires."}, {"question": "What are the payment options and do you offer EMI?", "answer": "We accept UPI (GPay/PhonePe/Paytm), Cash, Debit/Credit Cards, and 0% Interest EMI financing options for major treatments like Dental Implants, Braces, and Smile Makeovers."}], "policies": {"appointment_policy": "Prior appointment recommended to guarantee zero waiting time. Emergency toothache cases accommodated immediately.", "cancellation_policy": "Kindly give at least 2 hours notice if you need to reschedule or postpone your appointment.", "payment_policy": "UPI, Cards, Cash, and 0% EMI financing options available for major procedures."}, "appointment_rules": {"appointment_required": true, "allow_same_day_booking": true, "appointment_duration_minutes": 30, "appointment_buffer_minutes": 15, "default_visit_type": "in_clinic_consultation", "confirmation_required": true, "reminder_before_hours": 24}, "whatsapp_ai_instructions": {"tone": "Warm, polite, reassuring, empathetic, and expert healthcare coordinator.", "booking_instructions": "Empathize with the patient''s symptom or question (e.g. toothache, missing teeth, braces, cleaning), offer immediate reassurance that treatment is painless, and provide available appointment slots with Dr. P. Sudhakar or Dr. K. Mounika at the clinic."}}',
  NOW(6)
)
ON DUPLICATE KEY UPDATE
  value = VALUES(value),
  updated_at = NOW(6);
