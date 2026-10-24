const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const targetStr = `  /* ================= render ================= */`;

const startIndex = content.indexOf(targetStr);

if (startIndex === -1) {
  console.log('Failed to find target');
  process.exit(1);
}

const replacement = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/workspace.txt', 'utf8');

const newContent = content.substring(0, startIndex) + replacement + '\n';
fs.writeFileSync(file, newContent, 'utf8');
console.log('Successfully applied multi-page workspace');
