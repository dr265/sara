const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const targetStr = `                    {/* Header */}
                    <div className="p-6 border-b border-white/10 shrink-0">`;

const endTargetStr = `                    </div>
                  </>
                ) : (`;

const startIndex = content.indexOf(targetStr);
const endIndex = content.indexOf(endTargetStr, startIndex);

if (startIndex === -1 || endIndex === -1) {
  console.log('Failed to find targets');
  process.exit(1);
}

const replacement = fs.readFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/right_col_update.txt', 'utf8');

const newContent = content.substring(0, startIndex) + replacement + content.substring(endIndex);
fs.writeFileSync(file, newContent, 'utf8');
console.log('Successfully applied tabs via file text');
