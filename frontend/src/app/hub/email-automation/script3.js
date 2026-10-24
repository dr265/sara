const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const targetStr = `  /* ================= render ================= */`;
const startIndex = content.indexOf(targetStr);

const stateContent = content.substring(0, startIndex);
fs.writeFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page_head.txt', stateContent, 'utf8');
