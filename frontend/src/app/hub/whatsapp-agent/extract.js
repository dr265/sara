const fs = require('fs');
const content = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/whatsapp-agent/page.tsx', 'utf8');
const searchStr = 'const css = `';
const start = content.indexOf(searchStr);
if (start !== -1) {
  const end = content.indexOf('`;', start);
  const css = content.substring(start + searchStr.length, end);
  console.log('CSS size:', css.length);
  fs.writeFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/whatsapp-agent/css2.txt', css, 'utf8');
}
