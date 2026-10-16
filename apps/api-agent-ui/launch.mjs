import { launch } from './server.mjs';
launch().catch(() => { process.stderr.write('studio_host_start_failed\n'); process.exitCode = 1; });
