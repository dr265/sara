const fs = require('fs');

const waPath = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/whatsapp-agent/page.tsx';
const tgPath = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx';

let content = fs.readFileSync(waPath, 'utf8');

// 1. Rename Component and imports
content = content.replace(/WhatsAppHub/g, 'TelegramDashboard');
content = content.replace(/WHATSAPP_API_URL/g, 'LEGACY_API_URL');
content = content.replace(/NEXT_PUBLIC_WHATSAPP_API_URL/g, 'NEXT_PUBLIC_LEGACY_API_URL');

// 2. Replace Text & Variables
// Use case-sensitive replace to catch all variations
content = content.replace(/WhatsApp/g, 'Telegram');
content = content.replace(/whatsapp/g, 'telegram');
content = content.replace(/WHATSAPP/g, 'TELEGRAM');

// 3. Replace Colors
content = content.replace(/#0ac850/gi, '#00c8ff'); // primary cyan
content = content.replace(/#08a640/gi, '#00a2cc'); // hover cyan
content = content.replace(/rgba\(10,\s*200,\s*80/gi, 'rgba(0, 200, 255');
content = content.replace(/rgba\(10,200,80/gi, 'rgba(0,200,255');

// 4. In Telegram, the phone mockup should say Telegram instead of WhatsApp
// We already did this via WhatsApp -> Telegram replace.

// 5. Fix API Endpoints
// whatsapp-agent calls \`\${LEGACY_API_URL}/telegram/...\`. 
// We saw in backend telegram_agent.py that routes are at root, e.g. \`/knowledge/...\`.
// Actually, earlier in telegram-assistant we saw calls to \`\${LEGACY_API_URL}/agent/bootstrap\`
// So \`\${LEGACY_API_URL}/telegram/\` should become \`\${LEGACY_API_URL}/agent/\`
content = content.replace(/\$\{LEGACY_API_URL\}\/telegram\//g, '${LEGACY_API_URL}/agent/');
// except maybe for /health which is /agent/health
content = content.replace(/\/agent\/health/g, '/agent/health'); // Just in case
content = content.replace(/\/agent\/api\/demo/g, '/agent/conversations/message');
content = content.replace(/\/agent\/api\/owner/g, '/agent/conversations/owner');

// 6. Fix "connect phone" vs "connect bot"
content = content.replace(/Phone Numbers/g, 'Bot Tokens');
content = content.replace(/Connect Phone/g, 'Connect Bot');
content = content.replace(/Scan QR code/g, 'Paste Bot Token');

fs.writeFileSync(tgPath, content, 'utf8');
console.log('Successfully cloned WhatsApp Agent UI to Telegram Agent!');
