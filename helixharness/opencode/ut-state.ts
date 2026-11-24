/** Guard the native direct-write surface; actual publication uses ut_artifacts.py. */
import fs from 'node:fs';
import path from 'node:path';
export interface UtWriteEvent {tool: string; sessionID: string; agent?: string; input: unknown;}
const PROTECTED = /(?:^|\/)docs\/specs\/active\/[^/]+\/(?:unittest-(?:queue|session|results|defects|summary|experience)\.md|worker-[1-9]\d*-(?:(?:result|defects)\.md|findings\.json))$/i;
export function isProtectedUtStatePath(value: string): boolean { return PROTECTED.test(value.replaceAll("\\", "/")); }
export async function guardUtStateWrite(event: UtWriteEvent, resolveRoot: () => Promise<string>, inspectRelativeAliases = true): Promise<void> {
  if (!['write', 'edit'].includes(event.tool) || !event.input || typeof event.input !== 'object') return;
  const input = event.input as {path?: unknown};
  if (typeof input.path !== 'string') return;
  // Lexical reserved paths require no session API. Resolve relative aliases only
  // in active UT orchestration/worker scope, so ordinary editing stays independent.
  if (isProtectedUtStatePath(path.normalize(input.path))) throw new Error('ut_atomic_write_required: Use scripts/ut_artifacts.py with same-directory .tmp staging. Direct write/edit blocked.');
  if (!path.isAbsolute(input.path) && !inspectRelativeAliases) return;
  const target = path.isAbsolute(input.path) ? path.normalize(input.path) : path.resolve(await resolveRoot(), input.path);
  const candidates = new Set<string>();
  let cursor = target;
  for (let depth = 0; depth < 40 && !candidates.has(cursor); depth++) {
    candidates.add(cursor);
    try {
      cursor = path.join(fs.realpathSync(path.dirname(cursor)), path.basename(cursor));
      candidates.add(cursor);
      if (!fs.lstatSync(cursor).isSymbolicLink()) break;
      cursor = path.resolve(path.dirname(cursor), fs.readlinkSync(cursor));
    } catch { break; } // Missing final files are covered by their lexical target.
  }
  if ([...candidates].some(isProtectedUtStatePath)) {
    throw new Error('ut_atomic_write_required: Write the complete content to <target>.tmp, then run the bound plugin scripts/ut_artifacts.py with --repo-root, --feature and write --name --source. Findings JSON uses findings --worker --source; aggregate generates the parent defects report. Direct write/edit was blocked before execution.');
  }
}
