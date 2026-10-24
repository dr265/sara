const fs = require('fs');
const content = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx', 'utf8');

const fnIdx = content.indexOf('export default function');
let idx = content.indexOf('return (', fnIdx);
while (idx !== -1) {
    console.log('Return at:', idx);
    console.log(content.substring(idx, idx + 100));
    idx = content.indexOf('return (', idx + 1);
}
