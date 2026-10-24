const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/edulearner-ai/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const mainIdx = content.indexOf('<main ');
console.log(content.substring(mainIdx - 100, mainIdx + 500));
