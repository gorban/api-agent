export function assertCoverageMembership(browserFiles, nodeFiles) {
  for (const name of ['app.js','appearance.js','markdown.js','reply.js']) {
    if (!browserFiles.includes(name)) throw Error('Missing authored browser coverage: '+name);
  }
  for (const name of ['catalog.js','transport.js','server.mjs','launch.mjs']) {
    if (!nodeFiles.includes(name)) throw Error('Missing authored Node coverage: '+name);
  }
}
export function assertCoverageHashes(observations) {
  const hashes=new Map();
  for(const observation of observations) for(const [file,coverage] of Object.entries(observation)) {
    if(typeof coverage.hash!=='string' || !coverage.hash) throw Error('Missing coverage source identity: '+file);
    if(hashes.has(file) && hashes.get(file)!==coverage.hash) throw Error('Coverage source changed during verification: '+file);
    hashes.set(file,coverage.hash);
  }
}
