"""
Serves the Privacy Policy page required by Meta for WhatsApp Business API
app review/verification.
"""

PRIVACY_POLICY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Privacy Policy - Würth UAE WhatsApp Assistant</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #1a1a1a; }
  h1 { font-size: 1.6em; }
  h2 { font-size: 1.2em; margin-top: 2em; }
  .updated { color: #666; font-size: 0.9em; }
  li { margin-bottom: 0.4em; }
</style>
</head>
<body>

<h1>Privacy Policy - Würth UAE WhatsApp Assistant</h1>
<p class="updated">Last updated: 15 July 2026</p>

<p>
This Privacy Policy explains how <strong>Würth Gulf FZE</strong>
("we", "us", "our") collects, uses, and protects information when you interact with our
WhatsApp Business messaging assistant ("the Service").
</p>

<h2>1. Information we collect</h2>
<p>When you message us on WhatsApp, we may collect and store:</p>
<ul>
  <li>Your WhatsApp phone number</li>
  <li>The content of the messages you send us</li>
  <li>Your company name, if you provide it, and your assigned sales representative's
      name, phone number, and email (looked up from our internal customer records)</li>
  <li>Timestamps and basic conversation history, to provide context for follow-up replies</li>
</ul>
<p>We do not request or knowingly collect sensitive personal data (such as payment card
details, government ID numbers, or health information) through this Service.</p>

<h2>2. How we use your information</h2>
<ul>
  <li>To respond to your questions about Würth's products and services</li>
  <li>To identify your company and connect you with your assigned sales representative's
      contact details</li>
  <li>To escalate certain conversations (e.g. pricing requests, complaints, requests to
      speak with a human) to the relevant sales representative or staff member</li>
  <li>To improve the accuracy and relevance of our automated responses</li>
</ul>

<h2>3. How your information is processed</h2>
<p>To provide this Service, message content is shared with the following third-party
processors strictly to generate and deliver replies:</p>
<ul>
  <li><strong>Meta / WhatsApp Business Platform</strong> - to send and receive WhatsApp
      messages (see <a href="https://www.whatsapp.com/legal/business-data-processing-terms">
      WhatsApp Business Data Processing Terms</a>)</li>
  <li><strong>OpenRouter</strong> (openrouter.ai) - an AI model routing service used to
      generate reply text based on your message and our knowledge base. Message content
      is sent to OpenRouter's API and, in turn, to the underlying AI model provider to
      produce a response.</li>
  <li><strong>Google Sheets / Google Cloud</strong> - used internally to look up which
      sales representative is assigned to your company.</li>
</ul>
<p>We do not sell your personal data to third parties, and we do not use your data for
advertising or marketing profiling.</p>

<h2>4. Data storage and retention</h2>
<p>Your phone number, company association, and conversation history are stored in our
database for as long as reasonably necessary to provide the Service and maintain accurate
customer/sales-representative records, or as required by applicable law. You may request
deletion of your data at any time (see Section 7).</p>

<h2>5. Your rights</h2>
<p>Depending on your jurisdiction (including under the UAE Personal Data Protection Law
- Federal Decree-Law No. 45 of 2021), you may have the right to:</p>
<ul>
  <li>Request access to the personal data we hold about you</li>
  <li>Request correction of inaccurate data</li>
  <li>Request deletion of your data</li>
  <li>Withdraw consent to further messaging at any time</li>
</ul>

<h2>6. Opting out</h2>
<p>You can stop receiving messages from this Service at any time by replying "STOP",
blocking the WhatsApp Business number, or contacting us using the details below.</p>

<h2>7. Contact us</h2>
<p>
For any privacy-related questions, data access, or deletion requests, contact:<br>
<strong>Würth Gulf FZE</strong><br>
Email: <a href="mailto:eshop@wurth.ae">eshop@wurth.ae</a> or
<a href="mailto:CustomerHappinessCenter@wurth.ae">CustomerHappinessCenter@wurth.ae</a><br>
Address: P.O. Box 17036, Jebel Ali Free Zone, South 6, Dubai, U.A.E.<br>
Phone: +971 800 98784
</p>

<h2>8. Changes to this policy</h2>
<p>We may update this Privacy Policy from time to time. Changes will be posted on this
page with an updated "Last updated" date.</p>

</body>
</html>
"""
