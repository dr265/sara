const fs = require('fs');
let c = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx', 'utf8');
c = c.split('\\`').join('`');
c = c.split('\\$').join('$');
fs.writeFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx', c, 'utf8');
