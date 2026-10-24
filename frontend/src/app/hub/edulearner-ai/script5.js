const fs = require('fs');
const content = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/edulearner-ai/page.tsx', 'utf8');

const fnIdx = content.indexOf('export default function EduLearnerPage()');

let retIdx = content.indexOf('return (', fnIdx);
while (retIdx !== -1) {
   const excerpt = content.substring(retIdx, retIdx + 60);
   if (excerpt.includes('<div') || excerpt.includes('<main')) {
       console.log('Found main return at', retIdx);
       console.log(content.substring(retIdx, retIdx + 200));
       break;
   }
   retIdx = content.indexOf('return (', retIdx + 6);
}
