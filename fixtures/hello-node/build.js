const fs = require('fs');
fs.writeFileSync('artifact.txt', `built on ${process.version}\n`);
console.log(`self-test build OK on ${process.version}`);
