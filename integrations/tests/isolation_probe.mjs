import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
if (process.argv[2] === 'wait') {
  console.log(JSON.stringify({pid: process.pid}));
  setInterval(() => {}, 1000);
} else {
  const result = {};
  for (const [name, execute] of [
    ['read', () => readFileSync(process.argv[3])],
    ['write', () => writeFileSync(process.argv[3], 'forbidden')],
    ['spawn', () => { const child = spawnSync(process.execPath, ['-e', 'process.exit(0)'], {windowsHide:true}); if (child.error) throw child.error; if (child.status !== 0) throw new Error(`child status ${child.status}`); }],
  ]) {
    try { execute(); result[name] = 'allowed'; } catch (error) { result[name] = error.code ?? error.message; }
  }
  console.log(JSON.stringify(result));
}
