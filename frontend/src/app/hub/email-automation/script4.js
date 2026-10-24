const fs = require('fs');
const head = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page_head.txt', 'utf8');
const mockup = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/restored_mockup.txt', 'utf8');

const newContent = head + mockup;
fs.writeFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page.tsx', newContent, 'utf8');
console.log('Restored mockup successfully');
