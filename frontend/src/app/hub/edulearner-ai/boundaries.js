const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/edulearner-ai/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const returnIdx = content.indexOf('    return (\n      <div className="el-root');
const panelIdx = content.indexOf('      <section\n        role="tabpanel"');

console.log('Return idx:', returnIdx);
console.log('Panel idx:', panelIdx);
if (returnIdx > -1 && panelIdx > -1) {
   console.log('Structure between return and panel is:');
   console.log(content.substring(returnIdx, panelIdx));
}
