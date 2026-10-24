const fs = require('fs');
const content = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/whatsapp-agent/page.tsx', 'utf8');

const fnIdx = content.indexOf('export default function WhatsAppHub()');
const rootIdx = content.indexOf('<main className="sara', fnIdx);

if (rootIdx !== -1) {
   console.log(content.substring(rootIdx - 100, rootIdx + 1500));
} else {
   console.log('Could not find <main className="sara');
}
