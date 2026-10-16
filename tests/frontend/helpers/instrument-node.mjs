// Run the existing pure reply assertions under the same AST metric as the browser.
import {registerHooks} from 'node:module';
import {writeFileSync} from 'node:fs';
const {createInstrumenter}=await import(process.env.STUDIO_ISTANBUL_MODULE || 'istanbul-lib-instrument');
const instrumenter=createInstrumenter({esModules:true,compact:false,coverageGlobalScope:'globalThis',coverageGlobalScopeFunc:false});
const target=new URL('../../../apps/api-agent-ui/public/reply.js',import.meta.url);
registerHooks({load(url,context,nextLoad){
  const result=nextLoad(url,context);
  if(url===target.href)return {...result,source:instrumenter.instrumentSync(typeof result.source==='string'?result.source:new TextDecoder().decode(result.source),target.pathname)};
  return result;
}});
process.on('exit',()=>{
  if(globalThis.__coverage__)writeFileSync(new URL('node-ast-'+process.pid+'.json',process.env.STUDIO_NODE_AST_DIR),JSON.stringify(globalThis.__coverage__));
});
