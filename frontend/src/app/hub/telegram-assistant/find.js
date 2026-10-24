const fs = require('fs');
const content = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx', 'utf8');
const ret = content.indexOf('  return (\n    <div');
console.log(content.substring(ret, ret + 3000));
