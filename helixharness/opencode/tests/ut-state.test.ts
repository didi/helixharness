import {afterAll,expect,test} from 'bun:test';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import {guardUtStateWrite, isProtectedUtStatePath} from '../ut-state.ts';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'helix-ut-guard-'));
const directory=path.join(root,'docs/specs/active/example');fs.mkdirSync(directory,{recursive:true});
afterAll(()=>fs.rmSync(root,{recursive:true,force:true}));
const bind=async()=>root;
for(const name of ['unittest-queue.md','unittest-session.md','unittest-results.md','unittest-summary.md','unittest-defects.md','unittest-experience.md','worker-1-result.md','worker-2-defects.md','worker-3-findings.json']){
 for(const tool of ['write','edit'])test(`blocks ${tool} to protected ${name} before first write`,async()=>{
  await expect(guardUtStateWrite({tool,sessionID:'synthetic',input:{path:`docs/specs/active/example/${name}`}},bind)).rejects.toThrow('ut_atomic_write_required');
  expect(fs.existsSync(path.join(directory,name))).toBe(false);
 });
}
test('allows staging and ordinary business files without changing their input',async()=>{
 for(const p of ['docs/specs/active/example/unittest-results.md.tmp','src/main/Rules.java','docs/specs/active/example/spec.md','docs/notes/unittest-results.md']){
  const e={tool:'write',sessionID:'synthetic',input:{path:p,content:'literal'}};await guardUtStateWrite(e,bind);expect(e.input.content).toBe('literal');
 }
});
test('protects an absolute path and an alias to a protected directory',async()=>{
 const alias=path.join(root,'alias');fs.symlinkSync(directory,alias,'dir');
 for(const p of [path.join(directory,'unittest-results.md'),path.join(alias,'unittest-results.md')])await expect(guardUtStateWrite({tool:'write',sessionID:'synthetic',input:{path:p}},bind)).rejects.toThrow('ut_atomic_write_required');
});
test('ignores read and shell calls; never parses or rewrites arbitrary shell code',async()=>{
 for(const tool of ['read','shell'])await guardUtStateWrite({tool,sessionID:'synthetic',input:{path:path.join(directory,'unittest-results.md')}},async()=>{throw Error('must not look up session')});
});
test('relative writes use the requesting session directory, not plugin cwd',async()=>{
 const other=path.join(root,'other');fs.mkdirSync(other);
 await expect(guardUtStateWrite({tool:'write',sessionID:'B',input:{path:'../docs/specs/active/example/worker-1-result.md'}},async()=>other)).rejects.toThrow('ut_atomic_write_required');
});

test('recognizes platform-normalized Windows UT paths',()=>{
  expect(isProtectedUtStatePath(String.raw`C:\repo\docs\specs\active\sample\unittest-summary.md`)).toBe(true);
  expect(isProtectedUtStatePath(String.raw`C:\repo\src\Rules.java`)).toBe(false);
});
test('protects existing, dangling and chained leaf file aliases',async()=>{
 const target=path.join(directory,'unittest-summary.md');fs.writeFileSync(target,'complete');
 const alias=path.join(root,'report-alias.md');fs.symlinkSync(target,alias);
 const dangling=path.join(root,'dangling-alias.md');fs.symlinkSync(path.join(directory,'unittest-session.md'),dangling);
 const chained=path.join(root,'chain-alias.md');fs.symlinkSync(alias,chained);
 for(const p of [alias,dangling,chained])await expect(guardUtStateWrite({tool:'write',sessionID:'synthetic',input:{path:p}},bind)).rejects.toThrow('ut_atomic_write_required');
 expect(fs.readFileSync(target,'utf8')).toBe('complete');
});
test('allows ordinary file symlinks without rewriting tool input',async()=>{
 const target=path.join(root,'ordinary.txt');fs.writeFileSync(target,'original');const alias=path.join(root,'ordinary-link.txt');fs.symlinkSync(target,alias);
 await guardUtStateWrite({tool:'write',sessionID:'synthetic',input:{path:alias}},bind);
 expect(fs.readFileSync(target,'utf8')).toBe('original');
});

test('protects casing variants of reserved paths while allowing ordinary names',async()=>{
 for(const p of ['DOCS/SPECS/ACTIVE/Example/UNITTEST-SUMMARY.MD','docs/specs/active/example/Worker-1-Findings.JSON'])await expect(guardUtStateWrite({tool:'write',sessionID:'s',input:{path:p}},bind)).rejects.toThrow('ut_atomic_write_required');
 expect(isProtectedUtStatePath('docs/notes/UNITTEST-SUMMARY.MD')).toBe(false);
});
test('ordinary relative editing outside UT never depends on session API',async()=>{
 for(const tool of ['write','edit'])await guardUtStateWrite({tool,sessionID:'ordinary',input:{path:'src/Rules.java'}},async()=>{throw Error('session unavailable')},false);
});
test('UT relative aliases still fail closed when session directory is unavailable',async()=>{
 await expect(guardUtStateWrite({tool:'write',sessionID:'ut',input:{path:'report-link.md'}},async()=>{throw Error('session unavailable')},true)).rejects.toThrow('session unavailable');
});
